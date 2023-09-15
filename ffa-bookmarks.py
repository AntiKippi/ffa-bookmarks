#!/usr/bin/env python3

######################################################
# ffa-bookmarks
# Utility to manage bookmarks in Firefox for Android
#
# Author: Kippi
# Version: 0.0.0
######################################################

import argparse
import json
import os
import sqlite3
import tempfile
from adb_shell.adb_device import AdbDeviceTcp, AdbDeviceUsb
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from contextlib import closing
from enum import StrEnum, auto


class Format(StrEnum):
    HTML = auto(),
    JSON = auto()

VALID_FORMATS = [f.value for f in Format]
DEFAULT_FORMAT = Format.JSON

DB_FILE_NAME = 'places.sqlite'
ANDROID_TEMP_DIR = '/data/local/tmp'
DB_TEMP_FILE = f'{ANDROID_TEMP_DIR}/{DB_FILE_NAME}'

BASE_QUERY = 'SELECT mb.guid, mb.title, mb.position, mb.dateAdded, mb.lastModified, mb.id, mb.type, mp.url FROM moz_bookmarks mb LEFT OUTER JOIN moz_places mp ON mb.fk = mp.id'
ROOT_QUERY = f'{BASE_QUERY} WHERE mb.parent IS NULL'
CHILDREN_QUERY = f'{BASE_QUERY} WHERE mb.parent = ?'

TYPE_LOOKUP = {
    1: 'text/x-moz-place',
    2: 'text/x-moz-place-container'
}

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

HTML_HEAD = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<!-- This is an automatically generated file.
     It will be read and overwritten.
     DO NOT EDIT! -->
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self'; script-src 'none'; img-src data: *; object-src 'none'"></meta>
<TITLE>Bookmarks</TITLE>"""


def to_html(bookmarks):
    def add_node(folder):
        html_node = ''
        if folder['children']:
            for child in folder['children']:
                html_node += add_node(child)

    bookmark_menu = [node for node in bookmarks['children'] if node['guid'] == ROOT[1]]
    bookmark_toolbar = [node for node in bookmarks['children'] if node['guid'] == ROOT[2]]
    bookmark_other = [node for node in bookmarks['children'] if node['guid'] == ROOT[3]]
    rest = [node for node in bookmarks['children'] if node['guid'] != ROOT[1] and node['guid'] != ROOT[2] and node['guid'] != ROOT[3]]

    html = f"""{HTML_HEAD}
<H1>Bookmarks Menu</H1>

<DL><p>
    {''.join([add_node(child) for child in bookmark_menu['children']])}
    <DT><H3 ADD_DATE="{bookmark_toolbar['dateAdded'] >> 3}" LAST_MODIFIED="{bookmark_toolbar['lastModified'] >> 3}" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks Toolbar</H3>
    <DL><p>
        {''.join([add_node(child) for child in bookmark_toolbar['children']])}
    </DL><p>
    <DT><H3 ADD_DATE="{bookmark_other['dateAdded'] >> 3}" LAST_MODIFIED="{bookmark_other['lastModified'] >> 3}" UNFILED_BOOKMARKS_FOLDER="true">Other Bookmarks</H3>
    <DL><p>
        {''.join([add_node(child) for child in bookmark_other['children']])}
    </DL><p>
</DL>"""


def process_node(conn, node):
    node_dict = {
        'guid': node[0],
        'title': node[1],
        'index': node[2],
        'dateAdded': node[3] << 3,
        'lastModified': node[4] << 3,
        'id': node[5],
        'typeCode': node[6],
        'type': TYPE_LOOKUP[node[6]]
    }

    with closing(conn.cursor()) as cursor:
        with closing(cursor.execute(CHILDREN_QUERY, (node[5],))) as res:
            children = res.fetchall()
            children_nodes = [process_node(conn, n) for n in children]

    if node[0] in ROOT_LOOKUP:
        node_dict['root'] = ROOT_LOOKUP[node[0]]

    if node[7] is not None:
        node_dict['uri'] = node[7]

    if children_nodes:
        node_dict['children'] = children_nodes

    return node_dict


def set_fileformat(filename, fformat):
    if fformat:
        return Format(fformat)
    else:
        ext = filename.split('.')[-1]
        return Format(ext) if ext in VALID_FORMATS else DEFAULT_FORMAT


def main():
    ff_package_name = "org.mozilla.firefox"
    fileformat = None
    infile = None
    outfile = None
    dbfile = None
    privkey = os.path.join(os.path.expanduser('~'), '.android', 'adbkey')
    pubkey = f'{privkey}.pub'

    parser = argparse.ArgumentParser(description='Manage your bookmarks in Firefox for Android')
    parser.add_argument('-p',
                        '--package-name',
                        type=str,
                        action='store',
                        dest='ff_package_name',
                        required=False,
                        default=ff_package_name,
                        help=f'Specify the Firefox package name. Defaults to "{ff_package_name}".')
    parser.add_argument('-f',
                        '--format',
                        type=str.lower,
                        action='store',
                        dest='fformat',
                        required=False,
                        default=fileformat,
                        choices=VALID_FORMATS,
                        help=f'Specify the output format. If omitted, the format is determined from the outfile extension and falls back to {DEFAULT_FORMAT}.')
    parser.add_argument('-d',
                        '--db-file',
                        type=str,
                        action='store',
                        dest='dbfile',
                        required=False,
                        default=dbfile,
                        help=f'Use this sqlite db instead of fetching it from the device')
    parser.add_argument('--public-key',
                        type=str,
                        action='store',
                        dest='pubkey',
                        required=False,
                        default=pubkey,
                        help=f'The public key file to use. Defaults to {pubkey}.')
    parser.add_argument('--private-key',
                        type=str,
                        action='store',
                        dest='privkey',
                        required=False,
                        default=privkey,
                        help=f'The private key file to use. Defaults to {privkey}.')
    command_group = parser.add_mutually_exclusive_group(required=True)
    command_group.add_argument('-i',
                               '--import',
                               type=str,
                               action='store',
                               dest='infile',
                               default=infile,
                               help='Import the booksmarks from INFILE')
    command_group.add_argument('-e',
                               '--export',
                               type=str,
                               action='store',
                               nargs='?',
                               const='',
                               dest='outfile',
                               default=outfile,
                               help='Export the bookmarks to OUTFILE. If OUTFILE is omitted the results are written to standard output.')

    args = parser.parse_args()
    ff_package_name = args.ff_package_name
    fileformat = set_fileformat(infile or outfile or '', args.fformat)
    infile = args.infile
    outfile = args.outfile
    dbfile = args.dbfile
    pubkey = args.pubkey
    privkey = args.privkey

    if dbfile is None:
        # Load the public and private keys
        with open(privkey) as f:
            priv = f.read()
        with open(pubkey) as f:
            pub = f.read()
        signer = PythonRSASigner(pub, priv)

        # Connect via USB
        device = AdbDeviceUsb()
        device.connect(rsa_keys=[signer])

        # Copy db into tmp
        get_db_commands = f'cp \'\\\'\'/data/data/{ff_package_name}/files/{DB_FILE_NAME}\'\\\'\' \'\\\'\'{DB_TEMP_FILE}\'\\\'\';' + \
                          f'chown shell:shell \'\\\'\'{DB_TEMP_FILE}\'\\\'\';'

        print(device.shell(f'su -c \'{get_db_commands}\''))

        # Copy db to host and open it
        tmpdir = tempfile.TemporaryDirectory()
        dbfile = os.path.join(tmpdir.name, DB_FILE_NAME)
        device.pull(DB_TEMP_FILE, dbfile)

    with sqlite3.connect(dbfile) as conn:
        with closing(conn.cursor()) as cur:

            # The export flag has been used
            if outfile is not None:
                # Get the root node and build a bookmarks object
                with closing(cur.execute(ROOT_QUERY)) as res:
                    bookmarks = process_node(conn, res.fetchone())

                bookmarks_out = ''
                if fileformat == Format.JSON:
                    bookmark_out = json.dumps(bookmarks)
                elif fileformat == Format.HTML:
                    # Convert to HTML
                    bookmarks_out = 'HTML'
                else:
                    raise RuntimeError('No valid format given.')

                # Output the exported bookmarks
                if outfile == '':
                    print(bookmarks_out)
                else:
                    with open(outfile, 'w') as ofile:
                        ofile.write(bookmarks_out)

            # The import flag has been used
            elif infile is not None:
                print(dbfile)

            # This should not happen
            else:
                raise RuntimeError('No export or import flag given.')



if __name__ == '__main__':
    main()
