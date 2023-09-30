#!/usr/bin/env python3

######################################################
# ffa-bookmarks
# Utility to manage bookmarks in Firefox for Android
#
# Author: Kippi
# Version: 0.0.3
######################################################

import argparse
import json
import os
import random
import shutil
import sqlite3
import string
import sys
import tempfile
import usb1
import xml.etree.ElementTree as ET

from adb_shell.adb_device import AdbDeviceTcp, AdbDeviceUsb
from adb_shell.auth.keygen import keygen, write_public_keyfile
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from contextlib import closing
from enum import Enum, StrEnum, auto
from ppadb.client import Client as AdbClient
from ppadb.device import Device as PPAdbDevice


class Format(StrEnum):
    HTML = auto(),
    JSON = auto()


VALID_FORMATS = [f.value for f in Format]
DEFAULT_FORMAT = None

DB_FILE_NAME = 'places.sqlite'
WAL_EXTENSION = '-wal'
ANDROID_TEMP_DIR = '/data/local/tmp'
DB_TEMP_FILE = f'{ANDROID_TEMP_DIR}/{DB_FILE_NAME}'

ADB_DEVICE_DEFAULT_PORT = 5555
ADB_SERVER_DEFAULT_PORT = 5037

END_TAG_NAME = string.whitespace + '>'

BASE_QUERY = 'SELECT mb.guid, mb.title, mb.position, mb.dateAdded, mb.lastModified, mb.id, mb.type, mp.url FROM moz_bookmarks mb LEFT OUTER JOIN moz_places mp ON mb.fk = mp.id'
NODE_QUERY = f'{BASE_QUERY} WHERE mb.guid = ?'
CHILDREN_QUERY = f'{BASE_QUERY} WHERE mb.parent = ?'

URL_EXISTS_QUERY = 'SELECT id FROM moz_places WHERE url = ?'
LAST_INSERTED_QUERY = 'SELECT last_insert_rowid()'
INSERT_PLACE_QUERY = 'INSERT INTO moz_places(url, guid, url_hash) VALUES (:url, :guid, 0)'
INSERT_BOOKMARK_QUERY = 'INSERT INTO moz_bookmarks (id, fk, type, parent, position, title, dateAdded, lastModified, guid) ' + \
                        'VALUES (:id, :fk, :typeCode, :parent, :index, :title, :dateAdded, :lastModified, :guid) ' + \
                        'ON CONFLICT DO UPDATE SET fk = :fk, type = :typeCode, parent = :parent, position = :index, title = :title, dateAdded = :dateAdded, lastModified = :lastModified, guid = :guid'
INSERT_BOOKMARK_AUTOINCREMENT_QUERY = 'INSERT INTO moz_bookmarks (fk, type, parent, position, title, dateAdded, lastModified, guid) ' + \
                                      'VALUES (:fk, :typeCode, :parent, :index, :title, :dateAdded, :lastModified, :guid)'

TYPE_LOOKUP = {
    1: 'text/x-moz-place',
    2: 'text/x-moz-place-container'
}

# Key is id - 1
ROOT = [
    'root________',
    'menu________',
    'toolbar_____',
    'unfiled_____',
    'mobile______'
]

ROOT_LOOKUP = {
    ROOT[0]: 'placesRoot',
    ROOT[1]: 'bookmarksMenuFolder',
    ROOT[2]: 'toolbarFolder',
    ROOT[3]: 'unfiledBookmarksFolder',
    ROOT[4]: 'mobileFolder'
}

def bookmarks_to_html(bookmarks):
    def add_node(node, spaces):
        node['dateAdded'] = int(node['dateAdded'] / 1000)
        node['lastModified'] = int(node['lastModified'] / 1000)
        # Sanitize
        node['title'] = node['title'] \
            .replace('<', '&lt;') \
            .replace('>', '&gt;') \
            .replace('"', '&quot;') \
            .replace("'", '&#39;') \
            .replace('&', '&amp;') \
            .replace('"', '&quot;')
        if 'uri' in node:
            return f'{spaces * " "}<DT><A HREF="{node["uri"]}" ADD_DATE="{node["dateAdded"]}" LAST_MODIFIED="{node["lastModified"]}">{node["title"]}</A>\n'
        else:
            html_node = ''
            if node['guid'] != ROOT[1]:
                html_node += f'{spaces * " "}<DT><H3 ADD_DATE="{node["dateAdded"]}" LAST_MODIFIED="{node["lastModified"]}"'
                if node['guid'] == ROOT[2]:
                    html_node += ' PERSONAL_TOOLBAR_FOLDER="true"'
                    node['title'] = 'Bookmarks Toolbar'
                elif node['guid'] == ROOT[3]:
                    html_node += ' UNFILED_BOOKMARKS_FOLDER="true"'
                    node['title'] = 'Other Bookmarks'
                # This is a custom extension not found in the desktop version of Firefox
                elif node['guid'] == ROOT[4]:
                    html_node += ' MOBILE_BOOKMARKS_FOLDER="true"'
                    node['title'] = 'Mobile Bookmarks'
                html_node += f'>{node["title"]}</H3>\n'
                html_node += spaces * ' ' + "<DL><p>\n"
            if 'children' in node:
                for child in node['children']:
                    html_node += add_node(child, spaces + 4)
            if node['guid'] != ROOT[1]:
                html_node += spaces * " " + "</DL><p>\n"
            return html_node

    return f"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<!-- This is an automatically generated file.
     It will be read and overwritten.
     DO NOT EDIT! -->
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self'; script-src 'none'; img-src data: *; object-src 'none'"></meta>
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks Menu</H1>

<DL><p>
{''.join([add_node(child, 4) for child in bookmarks['children']])}
</DL>"""


def generate_guid():
    ALPHABET = string.ascii_letters + string.digits + '_-'
    return ''.join(random.choice(ALPHABET) for i in range(12))


def generate_node(guid, title, index, dateAdded, lastModified, id, typeCode, uri=None, children=None):
    node = {
        'guid': guid,
        'title': title,
        'index': index,
        'dateAdded': dateAdded,
        'lastModified': lastModified,
        'id': id,
        'typeCode': typeCode,
        'type': TYPE_LOOKUP[typeCode]
    }

    if node['guid'] in ROOT_LOOKUP:
        node['root'] = ROOT_LOOKUP[node['guid']]

    if uri is not None:
        node['uri'] = uri

    if children is not None:
        node['children'] = children

    return node


def get_url_id(conn, url):
    with closing(conn.execute(URL_EXISTS_QUERY, (url,))) as res:
        row = res.fetchone()
        if row:
            return row[0]
        else:
            tmpres = conn.execute(INSERT_PLACE_QUERY, (url, generate_guid()))
            tmpres.close()
            with closing(conn.execute(LAST_INSERTED_QUERY)) as res_rowid:
                return res_rowid.fetchone()[0]


# Note that the aim of the parser is not to be perfect but to be somewhat robust
def html_to_xmltree(html):
    class State(Enum):
        Initial = 0,
        InsideQuoteString = 1,
        InsideTag = 2,
        KillTag = 3,
        Found = 4

    # Turn html into list to change chars
    html = list(html)

    start = 0
    quote = None
    new_state = []
    state = State.Initial

    for i in range(0, len(html)):
        if state == State.InsideQuoteString:
            if html[i] == quote and html[i - 1] != '\\':
                state = new_state.pop()
                quote = None
            # Escape all <, > and & in strings
            elif html[i] == '>':
                html[i] = '&'
                html.insert(i + 1, 'gt;')
            elif html[i] == '<':
                html[i] = '&'
                html.insert(i + 1, 'lt;')
            elif html[i] == '&':
                html.insert(i + 1, 'amp;')
        elif state == State.KillTag:
            if html[i] == '>':
                state = new_state.pop()
            html[i] = ' '
        # If not inside a quote or in kill mode always go into quote mode when encountering one
        elif html[i] == '"' or html[i] == "'":
            new_state.append(state)
            state = State.InsideQuoteString
            quote = html[i]
        # Skip all the junk at the beginning until the first <DL>
        elif state == State.Initial:
            if html[i] == '<':
                if i < len(html) + 3 and html[i + 1].upper() == 'D' and html[i + 2].upper() == 'L' and html[
                    i + 3] in END_TAG_NAME:
                    # i should now be at the beginning of the outer <DL> block
                    start = i
                    new_state.append(state.Found)
                else:
                    new_state.append(state)
                state = State.InsideTag
        elif state == State.InsideTag:
            if html[i] == '>':
                state = new_state.pop()
        # Now kill all rouge <p> and <DL> tags
        elif state == State.Found:
            if html[i] == '<':
                new_state.append(state)
                if (i < len(html) + 2 and html[i + 1].upper() == 'P' and html[i + 2] in END_TAG_NAME) or \
                        (i < len(html) + 3 and html[i + 1].upper() == 'D' and html[i + 2].upper() == 'T' and html[
                            i + 3] in END_TAG_NAME):
                    html[i] = ' '
                    state = State.KillTag
                else:
                    state = State.InsideTag
        else:
            raise RuntimeError("Invalid state")

    # We found the root <DL>. html should now consist of only the big <DL>...</DL> block with all rouge tags killed
    if state == state.Found:
        html = ''.join(html[start:])
        return ET.fromstring(html)
    else:
        raise ValueError('Parser is in illegal end state, input probably invalid')


# Limitations (imposed due to the limitations of the HTML format):
#  - The browser just skips the Mobile Bookmarks folder when exporting to html
#  - The original id and guid are lost
#  - Due to the missing id/guid bookmarks cannot be overridden and will be duplicated if imported again
def import_xml(conn, html_xmltree, parent):
    def get_special_row_id(child, special_rows):
        nonlocal conn
        for row in special_rows:
            if row[0] in child.attrib and child.attrib[row[0]].lower() == 'true':
                with closing(conn.execute(NODE_QUERY, (row[1],))) as res:
                    return res.fetchone()[5]
        return None

    i = -1
    last_inserted_rowid = None
    for child in html_xmltree:
        if child.tag.upper() in ['A', 'H3']:
            url_id = None
            i += 1
            node_type = 2

            srowid = get_special_row_id(child, (('PERSONAL_TOOLBAR_FOLDER', ROOT[2]),
                                                ('UNFILED_BOOKMARKS_FOLDER', ROOT[3]),
                                                ('MOBILE_BOOKMARKS_FOLDER', ROOT[4])))
            if srowid is not None:
                last_inserted_rowid = srowid
                continue

            if child.tag.upper() == 'A':
                url_id = get_url_id(conn, child.attrib['HREF'])
                node_type = 1

            node = generate_node(generate_guid(),
                                 child.text,
                                 i,
                                 child.attrib['ADD_DATE'],
                                 child.attrib['LAST_MODIFIED'],
                                 None,
                                 node_type)

            node['fk'] = url_id
            node['parent'] = parent

            tmpres = conn.execute(INSERT_BOOKMARK_AUTOINCREMENT_QUERY, node)
            tmpres.close()

            with closing(conn.execute(LAST_INSERTED_QUERY)) as res_rowid:
                last_inserted_rowid = res_rowid.fetchone()[0]
        elif child.tag.upper() == 'DL':
            import_xml(conn, child, last_inserted_rowid)


def import_node(conn, node, parent=None):
    url_id = None
    if 'uri' in node:
        url_id = get_url_id(conn, node['uri'])

    node['dateAdded'] = int(node['dateAdded'] / 1000)
    node['lastModified'] = int(node['lastModified'] / 1000)
    node['fk'] = url_id
    node['parent'] = parent

    tmpres = conn.execute(INSERT_BOOKMARK_QUERY, node)
    tmpres.close()

    if 'children' in node:
        for child in node['children']:
            import_node(conn, child, node['id'])


# If conn evaluated to false no child nodes are appended
def export_node(conn, node):
    children_nodes = None
    if conn:
        with closing(conn.execute(CHILDREN_QUERY, (node[5],))) as res:
            children = res.fetchall()
            children_nodes = [export_node(conn, n) for n in children]

    return generate_node(node[0], node[1], node[2], node[3] * 1000, node[4] * 1000, node[5], node[6], node[7],
                         children_nodes)


def guess_fileformat(file):
    JSON_INDICATORS = ['{', '[', ']', '}']
    HTML_INDICATORS = ['<', '>']
    i = 0
    while file[i] not in JSON_INDICATORS + HTML_INDICATORS and i < len(file):
        i += 1

    return Format.HTML if file[i] in HTML_INDICATORS else (
        Format.JSON if file[i] in JSON_INDICATORS else DEFAULT_FORMAT)


def set_fileformat(filename, fformat):
    if fformat:
        return Format(fformat)
    else:
        ext = filename.split('.')[-1]
        return Format(ext) if ext in VALID_FORMATS else DEFAULT_FORMAT


_tmpdir = None
def get_tmpdir():
    global _tmpdir

    if _tmpdir is None:
        _tmpdir = tempfile.TemporaryDirectory()

    return _tmpdir


def initialize_adb_device(adb_tcp, privkey, pubkey, adb_server, adb_serial, force_server):
    dev = None

    def initialize_direct():
        nonlocal dev, adb_tcp, privkey, pubkey

        # Try loading the private key, try once again if it failed
        for i in range(0, 2):
            try:
                with open(privkey) as f:
                    priv = f.read()
            except FileNotFoundError:
                # If private key file doesn't exist, create it
                tmpdir = get_tmpdir()
                keypath = os.path.join(tmpdir.name, 'adbkey')
                keypath_pub = f'{keypath}.pub'

                # Create a public/private keypair at keypath and keypath.pub
                keygen(keypath)

                # Create any parent directories if necessary
                os.makedirs(os.path.dirname(privkey), exist_ok=True)

                # Move newly generated private key to privkey
                shutil.move(keypath, privkey)

                # Also create the public key if it does not exist
                # Note that this creates a TOCTOU race-condition
                # Should be fine in basically all cases though
                if os.path.isfile(pubkey):
                    sys.stderr.write(f'WARNING: Public key file "{pubkey}" already exists and will not be overwritten')
                    pubkey = keypath_pub
                else:
                    shutil.move(keypath_pub, pubkey)

                # Retry after key has been generated
                continue
            # Stop the loop if it was successful
            break

        # Try reading the public key file, try once again if it fails
        for i in range(0, 2):
            try:
                with open(pubkey) as f:
                    pub = f.read()
            # Just generate a public key if it does not exist
            except FileNotFoundError:
                # Create any parent directories if necessary
                os.makedirs(os.path.dirname(pubkey), exist_ok=True)

                # Create public keyfile
                write_public_keyfile(privkey, pubkey)

                # Retry after file has been generated
                continue
            # Stop the loop if it was successful
            break

        signer = PythonRSASigner(pub, priv)

        if adb_tcp is None:
            dev = AdbDeviceUsb()
        else:
            host, port = adb_tcp.split(':')
            dev = AdbDeviceTcp(host=host, port=int(port))

        dev.connect(rsa_keys=[signer])

    def initialize_server():
        nonlocal dev, adb_server, adb_serial

        host, port = adb_server.split(':')
        client = AdbClient(host=host, port=int(port))
        devices = client.devices()

        if len(devices) < 1:
            raise RuntimeError('No ADB devices available')
        elif len(devices) == 1:
            dev = devices[0]
        elif adb_serial is not None:
            dev = client.device(adb_serial)
        else:
            raise RuntimeError(f'No device selected. Available devices:\n{os.linesep.join([dev.serial for dev in devices])}')

    errors = []
    if force_server is None:
        # Try with adb_shell (native adb)
        try:
            initialize_direct()
        # Fall back to pure_python_adb if it doesn't work (needs running adb server)
        except Exception as device_error:
            # Collect the error to show it if the initialization via server also does not work
            errors.append(device_error)
            try:
                initialize_server()
            except Exception as server_error:
                errors.append(server_error)
    elif force_server:
        initialize_server()
    else:
        initialize_direct()

    if dev is None:
        raise RuntimeError(f'Could not initialize ADB device. Errors: {errors}')

    return dev


def main():
    _adb_device = None
    def get_adb_device():
        nonlocal _adb_device, adb_tcp, privkey, pubkey, adb_server, adb_serial, force_server

        if _adb_device is None:
            try:
                _adb_device = initialize_adb_device(adb_tcp, privkey, pubkey, adb_server, adb_serial, force_server)
            except usb1.USBError as e:
                sys.stderr.write(f'ERROR [ADB]: {e}.\nDevice might be blocked by your local adb server, try killing it.')
                exit(1)
            except Exception as e:
                sys.stderr.write(f'ERROR [ADB]: {e}')
                exit(1)

        return _adb_device

    ff_package_name = "org.mozilla.firefox"
    fileformat = None
    dbfile = None
    adb_tcp = None
    privkey = os.path.join(os.path.expanduser('~'), '.android', 'adbkey')
    pubkey = f'{privkey}.pub'
    adb_server = '127.0.0.1:5037'
    adb_serial = None
    force = False
    force_server = None

    infile = None
    outfile = None
    copydb = False

    parser = argparse.ArgumentParser(description='Manage your bookmarks in Firefox for Android')
    parser.add_argument('-p',
                        '--package-name',
                        type=str,
                        action='store',
                        dest='ff_package_name',
                        required=False,
                        default=ff_package_name,
                        help=f'Specify the Firefox package name. Defaults to "{ff_package_name}".')
    parser.add_argument('-t',
                        '--file-type',
                        type=str.lower,
                        action='store',
                        dest='fformat',
                        required=False,
                        default=fileformat,
                        choices=VALID_FORMATS,
                        help='Specify the file format. If omitted, the format is determined from the outfile extension or infile contents or JSON as fallback.')
    parser.add_argument('-d',
                        '--db-file',
                        type=str,
                        action='store',
                        dest='dbfile',
                        required=('-c' in sys.argv or '--copy' in sys.argv),
                        default=dbfile,
                        help='Use DBFILE instead of fetching the places.sqlite db from the device.')
    parser.add_argument('-a',
                        '--adb-device-tcp',
                        type=str,
                        action='store',
                        dest='adb_tcp',
                        required=False,
                        default=adb_tcp,
                        help=f'The address and port of the device in the format "host:port". Port defaults to {ADB_DEVICE_DEFAULT_PORT} if omitted.')
    parser.add_argument('--private-key',
                        type=str,
                        action='store',
                        dest='privkey',
                        required=False,
                        default=privkey,
                        help=f'The private key file to use. Defaults to "{privkey}". '
                             f'The file will be created if it does not exist.')
    parser.add_argument('--public-key',
                        type=str,
                        action='store',
                        dest='pubkey',
                        required=False,
                        default=pubkey,
                        help=f'The public key file to use. Defaults to "{pubkey}". '
                             f'The file will be created if it does not exist.')
    parser.add_argument('--adb_server',
                        type=str,
                        action='store',
                        dest='adb_server',
                        required=False,
                        default=adb_server,
                        help=f'The ADB server to use. Defaults to "{adb_server}".`')
    parser.add_argument('-s',
                        '--serial',
                        type=str,
                        action='store',
                        dest='adb_serial',
                        required=False,
                        default=adb_serial,
                        help='The serial number of the device to use as shown in `adb devices`.')
    parser.add_argument('-f',
                        '--force',
                        action='store_true',
                        dest='force',
                        required=False,
                        default=force,
                        help='Do not ask before overwriting files.')
    server_group = parser.add_mutually_exclusive_group(required=False)
    server_group.add_argument('--server',
                              action='store_true',
                              dest='force_server',
                              required=False,
                              help='Force the usage of the ADB server.')
    server_group.add_argument('--no-server',
                              action='store_false',
                              dest='force_server',
                              required=False,
                              help='Force a direct device connection via USB or TCP.')
    command_group = parser.add_mutually_exclusive_group(required=True)
    command_group.add_argument('-i',
                               '--import',
                               type=str,
                               action='store',
                               nargs='?',
                               const='',
                               dest='infile',
                               default=infile,
                               help='Import the booksmarks from INFILE. If INFILE is omitted stdin is used.')
    command_group.add_argument('-e',
                               '--export',
                               type=str,
                               action='store',
                               nargs='?',
                               const='',
                               dest='outfile',
                               default=outfile,
                               help='Export the bookmarks to OUTFILE. If OUTFILE is omitted the results are written to stdout.')
    command_group.add_argument('-c',
                               '--copy-db',
                               action='store_true',
                               dest='copydb',
                               default=copydb,
                               help=f'Copy the {DB_FILE_NAME} file to the DBFILE specified by -d.')

    args = parser.parse_args()
    infile = args.infile
    outfile = args.outfile
    copydb = args.copydb
    ff_package_name = args.ff_package_name.replace("'", "'\\''\\\\'\\'''\\''")  # Prevent command injection
    fileformat = set_fileformat(infile or outfile or '', args.fformat)
    dbfile = args.dbfile
    adb_tcp = args.adb_tcp
    if adb_tcp is not None:
        # Set default port if not specified
        if ':' not in adb_tcp:
            adb_tcp = f'{adb_tcp}:{ADB_DEVICE_DEFAULT_PORT}'
    pubkey = args.pubkey
    privkey = args.privkey
    if adb_server != args.adb_server:
        # If adb_server is given, we assume the user wants to connect via an ADB server
        # Note that this can be overridden with --no-server
        force_server = True
        adb_server = args.adb_server
        # Set default port if not specified
        if ':' not in adb_server:
            adb_tcp = f'{adb_tcp}:{ADB_SERVER_DEFAULT_PORT}'
    adb_serial = args.adb_serial
    if args.force_server is not None:
        force_server = args.force_server

    if copydb or dbfile is None:
        device = get_adb_device()

        # Copy db into tmp
        get_db_commands = f'cp \'\\\'\'/data/data/{ff_package_name}/files/{DB_FILE_NAME}\'\\\'\' \'\\\'\'{DB_TEMP_FILE}\'\\\'\';' + \
                          f'cp \'\\\'\'/data/data/{ff_package_name}/files/{DB_FILE_NAME}{WAL_EXTENSION}\'\\\'\' \'\\\'\'{DB_TEMP_FILE}{WAL_EXTENSION}\'\\\'\';' + \
                          f'chown shell:shell \'\\\'\'{DB_TEMP_FILE}\'\\\'\' \'\\\'\'{DB_TEMP_FILE}{WAL_EXTENSION}\'\\\'\';'
        device.shell(f'su -c \'{get_db_commands}\'')

        if dbfile is None:
            tmpdir = get_tmpdir()
            dbfile = os.path.join(tmpdir.name, DB_FILE_NAME)

        wal_file = f'{dbfile}{WAL_EXTENSION}'

        # Ask before overwriting dbfile and dbfile-wal (if force flag is not set)
        # Note that this creates a TOCTOU race-condition
        # Should be fine in basically all cases though
        if not force:
            if os.path.isfile(dbfile):
                # Use stderr for input to avoid cluttering stdout
                sys.stderr.write(f'File "{dbfile}" already exists, overwrite? [y/N]: ')
                resp = input().upper()
                if resp not in ['Y', 'YES']:
                    exit(1)
            if os.path.isfile(wal_file):
                # Use stderr for input to avoid cluttering stdout
                sys.stderr.write(f'File "{wal_file}" already exists, overwrite? [y/N]: ')
                resp = input().upper()
                if resp not in ['Y', 'YES']:
                    exit(1)

        # Copy db to host
        device.pull(DB_TEMP_FILE, dbfile)
        device.pull(f'{DB_TEMP_FILE}{WAL_EXTENSION}', wal_file)

        # Cleanup
        device.shell(f'rm -f \'{DB_TEMP_FILE}\'')

    # The export flag has been used
    if outfile is not None:
        with sqlite3.connect(dbfile) as conn:
            # Get the root node and build a bookmarks object
            with closing(conn.execute(NODE_QUERY, (ROOT[0],))) as res:
                bookmarks = export_node(conn, res.fetchone())

            bookmarks_out = ''
            if fileformat == Format.HTML:
                bookmarks_out = bookmarks_to_html(bookmarks)
            else:
                # Use JSON as default fallback
                bookmarks_out = json.dumps(bookmarks)

            # Append trailing newline if not present
            if bookmarks_out[-1] != '\n':
                bookmarks_out += '\n'

            # Output the exported bookmarks
            if outfile == '':
                print(bookmarks_out, end='')
            else:
                # Ask before overwriting (if force flag is not set)
                # Note that this creates a TOCTOU race-condition
                # Should be fine in basically all cases though
                if os.path.isfile(outfile) and not force:
                    # Use stderr for input to avoid cluttering stdout
                    # Not really needed here since write to file is guaranteed, but still stderr is used for consistency
                    sys.stderr.write(f'File "{outfile}" already exists, overwrite? [y/N]: ')
                    resp = input().upper()
                    if resp not in ['Y', 'YES']:
                        exit(1)

                with open(outfile, 'w') as ofile:
                    ofile.write(bookmarks_out)

    # The import flag has been used
    elif infile is not None:
        device = get_adb_device()

        if not force:
            server_connection = isinstance(device, PPAdbDevice)
            dev_str = device.get_serial_no() if server_connection else (adb_tcp if adb_tcp is not None else "USB")
            # Use stderr for input to avoid cluttering stdout
            # Not really needed here since we are importing, but still stderr is used for consistency
            sys.stderr.write('Please review before importing:\n'
                  f' - ADB Connection: {f"Server at {adb_server}" if server_connection else "Direct"}\n'
                  f' - ADB Device: {dev_str}\n'
                  f' - Firefox package name: {ff_package_name}\n'
                  f' - Import file: "{infile if infile is not None else "stdin"}"\n'
                  'Your existing bookmarks might be overridden. Continue? [y/N]: ')

            resp = input().upper()
            if resp not in ['Y', 'YES']:
                exit(1)

        with sqlite3.connect(dbfile) as conn:
            if infile == '':
                file_contents = sys.stdin.read()
            else:
                with open(infile, 'r') as ifile:
                    file_contents = ifile.read()

            if fileformat is None:
                fileformat = guess_fileformat(file_contents)

            if fileformat == Format.HTML:
                root = html_to_xmltree(file_contents)
                with closing(conn.execute(NODE_QUERY, (ROOT[1],))) as res:
                    bookmark_menu_id = res.fetchone()[5]
                import_xml(conn, root, bookmark_menu_id)
            # Use JSON as default fallback
            else:
                bookmarks = json.loads(file_contents)
                import_node(conn, bookmarks)
            conn.commit()

            # Copy the WAL content into the main db
            tmpres = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            tmpres.close()

        # Copy db to device
        device.push(dbfile, DB_TEMP_FILE)

        # Move db from tmp
        mv_db_commands = f'cp --preserve= \'\\\'\'{DB_TEMP_FILE}\'\\\'\' \'\\\'\'/data/data/{ff_package_name}/files/{DB_FILE_NAME}\'\\\'\';' + \
                         f'rm -f \'\\\'\'/data/data/{ff_package_name}/files/{DB_FILE_NAME}{WAL_EXTENSION}\'\\\'\';' + \
                         f'rm -f \'\\\'\'{DB_TEMP_FILE}\'\\\'\';'
        device.shell(f'su -c \'{mv_db_commands}\'')

    # This should not happen
    elif not copydb:
        raise RuntimeError('No export or import or copydb flag given.')


if __name__ == '__main__':
    main()
