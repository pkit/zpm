#  Copyright 2014 Rackspace, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import tarfile
import glob
import gzip
import json
import shlex
import fnmatch
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse
try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

from zpmlib import miniswift

import jinja2
import yaml

_DEFAULT_UI_TEMPLATES = ['index.html', 'style.css', 'zebra.js']


def create_project(location):
    """
    Create a ZeroVM application project by writing a default `zapp.yaml` in the
    specified directory `location`.

    :returns: Full path to the created `zapp.yaml` file.
    """
    if os.path.exists(location):
        if not os.path.isdir(location):
            # target must be an empty directory
            raise RuntimeError("Target `location` must be a directory")
    else:
        os.makedirs(location)
    return _create_zapp_yaml(location)


def render_zapp_yaml(name):
    """Load and render the zapp.yaml template."""
    loader = jinja2.PackageLoader('zpmlib', 'templates')
    env = jinja2.Environment(loader=loader)
    tmpl = env.get_template('zapp.yaml')
    return tmpl.render(name=name)


def _create_zapp_yaml(location):
    """
    Create a default `zapp.yaml` file in the specified directory `location`.

    Raises a `RuntimeError` if the `location` already contains a `zapp.yaml`
    file.
    """
    filepath = os.path.join(location, 'zapp.yaml')
    if os.path.exists(filepath):
        raise RuntimeError("'%s' already exists!" % filepath)

    with open(os.path.join(location, 'zapp.yaml'), 'w') as fp:
        name = os.path.basename(os.path.abspath(location))
        fp.write(render_zapp_yaml(name))

    return filepath


def find_project_root():
    root = os.getcwd()
    while not os.path.isfile(os.path.join(root, 'zapp.yaml')):
        oldroot, root = root, os.path.dirname(root)
        if root == oldroot:
            raise RuntimeError("no zapp.yaml file found")
    return root


def _generate_job_desc(zapp):
    job = []

    def make_file_list(zgroup):
        file_list = []
        for device in zgroup['devices']:
            dev = {'device': device['name']}
            if 'path' in device:
                dev['path'] = device['path']
            file_list.append(dev)
        return file_list

    # TODO(mg): we should eventually reuse zvsh._nvram_escape
    def escape(value):
        for c in '\\", \n':
            value = value.replace(c, '\\x%02x' % ord(c))
        return value

    def translate_args(cmdline):
        # On Python 2, the yaml module loads non-ASCII strings as
        # unicode objects. In Python 2.7.2 and earlier, we must give
        # shlex.split a str -- but it is an error to give shlex.split
        # a bytes object in Python 3.
        need_decode = not isinstance(cmdline, str)
        if need_decode:
            cmdline = cmdline.encode('utf8')
        args = shlex.split(cmdline)
        if need_decode:
            args = [arg.decode('utf8') for arg in args]
        return ' '.join(escape(arg) for arg in args)

    for zgroup in zapp['execution']['groups']:
        jgroup = {'name': zgroup['name']}
        jgroup['exec'] = {
            'path': zgroup['path'],
            'args': translate_args(zgroup['args']),
        }

        jgroup['file_list'] = make_file_list(zgroup)

        if 'connect' in zgroup:
            jgroup['connect'] = zgroup['connect']

        job.append(jgroup)
    return job


def _add_ui(tar, zapp):
    loader = jinja2.PackageLoader('zpmlib', 'templates')
    env = jinja2.Environment(loader=loader)

    for path in _DEFAULT_UI_TEMPLATES:
        tmpl = env.get_template(path)
        output = tmpl.render(zapp=zapp)
        # NOTE(larsbutler): Python 2.7.2 has a bug related to unicode and
        # cStringIO. To work around this, we need the following explicit
        # encoding. See http://bugs.python.org/issue1548891.
        output = output.encode('utf-8')
        info = tarfile.TarInfo(name=path)
        info.size = len(output)
        print('adding %s' % path)
        tar.addfile(info, BytesIO(output))


def _get_swift_zapp_url(swift_service_url, zapp_path):
    """
    :param str swift_service_url:
        The Swift service URL returned from a Keystone service catalog.
        Example: http://localhost:8080/v1/AUTH_469a9cd20b5a4fc5be9438f66bb5ee04

    :param str zapp_path:
        <container>/<zapp-file-name>. Example:

            test_container/myapp.zapp

    Here's a typical usage example, with typical input and output:

    >>> swift_service_url = ('http://localhost:8080/v1/'
    ...                      'AUTH_469a9cd20b5a4fc5be9438f66bb5ee04')
    >>> zapp_path = 'test_container/myapp.zapp'
    >>> _get_swift_zapp_url(swift_service_url, zapp_path)
    'swift://AUTH_469a9cd20b5a4fc5be9438f66bb5ee04/test_container/myapp.zapp'
    """
    swift_path = urlparse.urlparse(swift_service_url).path
    # TODO(larsbutler): Why do we need to check if the path contains '/v1/'?
    # This is here due to legacy reasons, but it's not clear to me why this is
    # needed.
    if swift_path.startswith('/v1/'):
        swift_path = swift_path[4:]
    return 'swift://%s/%s' % (swift_path, zapp_path)


def _prepare_job(tar, zapp, zapp_swift_url):
    """
    :param tar:
        The application .zapp file, as a :class:`tarfile.TarFile` object.
    :param dict zapp:
        Parsed contents of the application `zapp.yaml` specification, as a
        `dict`.
    :param str zapp_swift_url:
        Path of the .zapp in Swift, which looks like this::

            'swift://AUTH_abcdef123/test_container/hello.zapp'

        See :func:`_get_swift_zapp_url`.

    :returns:
        Extracted contents of the app json (example: hello.json) with the swift
        path to the .zapp added to the `file_list` for each `group`.

        So if the job looks like this::

            [{'exec': {'args': 'hello.py', 'path': 'file://python2.7:python'},
              'file_list': [{'device': 'python2.7'}, {'device': 'stdout'}],
              'name': 'hello'}]

        the output will look like something like this::

            [{'exec': {u'args': 'hello.py', 'path': 'file://python2.7:python'},
              'file_list': [
                {'device': 'python2.7'},
                {'device': 'stdout'},
                {'device': 'image',
                 'path': 'swift://AUTH_abcdef123/test_container/hello.zapp'},
              ],
              'name': 'hello'}]


    """
    fp = tar.extractfile('%s.json' % zapp['meta']['name'])
    # NOTE(larsbutler): the `decode` is needed for python3
    # compatibility
    job = json.loads(fp.read().decode('utf-8'))
    device = {'device': 'image', 'path': zapp_swift_url}
    for group in job:
        group['file_list'].append(device)

    return job


def bundle_project(root):
    """
    Bundle the project under root.
    """
    zapp_yaml = os.path.join(root, 'zapp.yaml')
    zapp = yaml.safe_load(open(zapp_yaml))

    zapp_name = zapp['meta']['name'] + '.zapp'

    tar = tarfile.open(zapp_name, 'w:gz')

    job = _generate_job_desc(zapp)
    job_json = json.dumps(job)
    info = tarfile.TarInfo(name='%s.json' % zapp['meta']['name'])
    # This size is only correct because json.dumps uses
    # ensure_ascii=True by default and we thus have a 1-1
    # correspondence between Unicode characters and bytes.
    info.size = len(job_json)

    print('adding %s' % info.name)
    # In Python 3, we cannot use a str or bytes object with addfile,
    # we need a BytesIO object. In Python 2, BytesIO is just StringIO.
    # Since json.dumps produces an ASCII-only Unicode string in Python
    # 3, it is safe to encode it to ASCII.
    tar.addfile(info, BytesIO(job_json.encode('ascii')))

    zapp['bundling'].append('zapp.yaml')
    ui = zapp.get('ui', [])
    for pattern in zapp['bundling'] + ui:
        for path in glob.glob(os.path.join(root, pattern)):
            print('adding %s' % path)
            relpath = os.path.relpath(path, root)
            info = tarfile.TarInfo(name=relpath)
            info.size = os.path.getsize(path)
            tar.addfile(info, open(path, 'rb'))

    if not ui:
        _add_ui(tar, zapp)

    tar.close()
    print('created %s' % zapp_name)


def _find_ui_uploads(zapp, tar):
    if 'ui' not in zapp:
        return _DEFAULT_UI_TEMPLATES

    matches = set()
    names = tar.getnames()
    for pattern in zapp['ui']:
        matches.update(fnmatch.filter(names, pattern))
    return matches


def deploy_project(args):
    tar = tarfile.open(args.zapp)
    zapp = yaml.safe_load(tar.extractfile('zapp.yaml'))

    client = miniswift.ZwiftClient(args.os_auth_url,
                                   args.os_tenant_name,
                                   args.os_username,
                                   args.os_password)
    client.auth()

    path = '%s/%s' % (args.target, os.path.basename(args.zapp))
    client.upload(path, gzip.open(args.zapp).read())

    swift_url = _get_swift_zapp_url(client._swift_service_url, path)

    job = _prepare_job(tar, zapp, swift_url)
    client.upload('%s/%s.json' % (args.target, zapp['meta']['name']),
                  json.dumps(job))

    # TODO(mg): inserting the username and password in the uploaded
    # file makes testing easy, but should not be done in production.
    # See issue #44.
    deploy = {'auth_url': args.os_auth_url,
              'tenant': args.os_tenant_name,
              'username': args.os_username,
              'password': args.os_password}
    for path in _find_ui_uploads(zapp, tar):
        # Upload UI files after expanding deployment parameters
        tmpl = jinja2.Template(tar.extractfile(path).read())
        output = tmpl.render(deploy=deploy)
        client.upload('%s/%s' % (args.target, path), output)

    if args.execute:
        print('job template:')
        from pprint import pprint
        pprint(job)
        print('executing')
        client.post_job(job)

    print('app deployed to\n  %s/%s/' % (client._swift_service_url,
                                         args.target))
