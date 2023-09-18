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
import random
import sqlite3
import string
import sys
import tempfile
import xml.etree.ElementTree as ET
from adb_shell.adb_device import AdbDeviceTcp, AdbDeviceUsb
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from contextlib import closing
from enum import Enum, StrEnum, auto


class Format(StrEnum):
    HTML = auto(),
    JSON = auto()

VALID_FORMATS = [f.value for f in Format]
DEFAULT_FORMAT = None

DB_FILE_NAME = 'places.sqlite'
ANDROID_TEMP_DIR = '/data/local/tmp'
DB_TEMP_FILE = f'{ANDROID_TEMP_DIR}/{DB_FILE_NAME}'

END_TAG_NAME = string.whitespace + '>'

BASE_QUERY = 'SELECT mb.guid, mb.title, mb.position, mb.dateAdded, mb.lastModified, mb.id, mb.type, mp.url FROM moz_bookmarks mb LEFT OUTER JOIN moz_places mp ON mb.fk = mp.id'
NODE_QUERY = f'{BASE_QUERY} WHERE mb.guid = ?'
CHILDREN_QUERY = f'{BASE_QUERY} WHERE mb.parent = ?'

URL_EXISTS_QUERY = 'SELECT id FROM moz_places WHERE url = ?'
INSERT_PLACE_QUERY = 'INSERT INTO moz_places(url, guid, url_hash) VALUES (:url, :guid, 0)'
INSERT_BOOKMARKS_QUERY = 'INSERT INTO moz_bookmarks (id, fk, type, parent, position, title, dateAdded, lastModified, guid) ' +\
                         'VALUES (:id, :fk, :typeCode, :parent, :index, :title, :dateAdded, :lastModified, :guid) ' +\
                         'ON CONFLICT DO UPDATE SET fk = :fk, type = :typeCode, parent = :parent, position = :index, title = :title, dateAdded = :dateAdded, lastModified = :lastModified, guid = :guid'

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


# Limitations (imposed due to the limitations of the HTML format):
#  - If a browser generated HTML file is used, the Mobile Bookmarks folder will be missing
#  - The original id and guid are lost
#  - Due to the missing id/guid bookmarks cannot be overridden and will be duplicated if imported again
# Also note that the aim of the parser is not to be perfect but to be somewhat robust
# bookmarks_root should be a python object structure of the root and bookmark menu folder
def html_to_bookmarks(html, bookmarks_root, max_id):
    class State(Enum):
        Initial = 0,
        InsideQuoteString = 1,
        InsideTag = 2,
        KillTag = 3,
        Found = 4

    def add_xml_node(xml_element, bookmark_folder, root):
        nonlocal max_id
        i = 0

        def mknode(child, uri=None):
            nonlocal max_id, i
            max_id += 1
            i += 1
            return generate_node(generate_guid(),
                                 child.text,
                                 i,
                                 child.attrib['ADD_DATE'],
                                 child.attrib['LAST_MODIFIED'],
                                 max_id,
                                 2 if uri is None else 1,
                                 uri,
                                 [] if uri is None else None)

        for child in xml_element:
            if child.tag.upper() == 'A':
                bookmark_folder.append(mknode(child, child['HREF']))
            elif child.tag.upper() == 'H3':
                bookmark_folder.append(mknode(child, child['HREF']))

    # Escape all ampersands
    html = html.replace('&', '&amp;')

    # Turn html into list to change chars
    html = list(html)

    start = 0
    quote = None
    new_state = []
    state = State.Initial

    for i in range(0, len(html)):
        if state == State.InsideQuoteString:
            if html[i] == quote and html[i-1] != '\\':
                state = new_state.pop()
                quote = None
            # Escape all < and > in strings
            elif html[i] == '>':
                html[i] = '&'
                html.insert(i+1, 'gt;')
            elif html[i] == '<':
                html[i] = '&'
                html[i+1].insert('lt;')
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
                if i < len(html) + 3 and html[i+1].upper() == 'D' and html[i+2].upper() == 'L' and html[i+3] in END_TAG_NAME:
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
                if (i < len(html) + 2 and html[i+1].upper() == 'P' and html[i+2] in END_TAG_NAME) or \
                        (i < len(html) + 3 and html[i+1].upper() == 'D' and html[i+2].upper() == 'T' and html[i+3] in END_TAG_NAME):
                    html[i] = ' '
                    state = State.KillTag
                else:
                    state = State.InsideTag
        else:
            raise RuntimeError("Invalid state")

    # We found the root <DL>. html should now consist of only the big <DL>...</DL> block with all rouge tags killed
    if state == state.Found or state == state.KillTag:
        html = ''.join(html[start:])
        xml_root = ET.fromstring(html)
        bookmark_menu = bookmarks_root['children'][0]
        bookmark_menu['children'] = []




def bookmarks_to_html(bookmarks):
    def add_node(node, spaces):
        if 'uri' in node:
            return f'{spaces * " "}<DT><A HREF="{node["uri"]}" ADD_DATE="{node["dateAdded"] / 1000}" LAST_MODIFIED="{node["lastModified"] / 1000}">{node["title"]}</A>\n'
        else:
            html_node = ''
            if node['guid'] != ROOT[1]:
                html_node += f'{spaces * " "}<DT><H3 ADD_DATE="{node["dateAdded"] / 1000}" LAST_MODIFIED="{node["lastModified"] / 1000}"'
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
                    html_node += add_node(child, spaces+4)
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

    if node[0] in ROOT_LOOKUP:
        node['root'] = ROOT_LOOKUP[node[0]]

    if uri is not None:
        node['uri'] = node[7]

    if children is not None:
        node['children'] = children

    return node


def import_node(conn, node, parent = None):
    with closing(conn.cursor()) as cursor:
        url_id = None
        if 'uri' in node:
            with closing(cursor.execute(URL_EXISTS_QUERY, (node['uri'],))) as res:
                row = res.fetchone()
                if row:
                    url_id = row[0]
                else:
                    tmpres = cursor.execute(INSERT_PLACE_QUERY, (node['uri'], generate_guid()))
                    tmpres.close()
                    with closing(cursor.execute('SELECT last_insert_rowid()')) as res_rowid:
                        url_id = res_rowid.fetchone()[0]

        node['dateAdded'] /= 1000
        node['lastModified'] /= 1000
        node['fk'] = url_id
        node['parent'] = parent

    with closing(conn.cursor()) as cursor:
        tmpres = cursor.executemany(INSERT_BOOKMARKS_QUERY, (node,))
        tmpres.close()

    if 'children' in node:
        for child in node['children']:
            import_node(conn, child, node['id'])


# If conn evaluated to false no child nodes are appended
def export_node(conn, node):
    children_nodes = None
    if conn:
        with closing(conn.cursor()) as cursor:
            with closing(cursor.execute(CHILDREN_QUERY, (node[5],))) as res:
                children = res.fetchall()
                children_nodes = [export_node(conn, n) for n in children]

    return generate_node(node[0], node[1], node[2], node[3] * 1000, node[4] * 1000, node[5], node[6], node[7], children_nodes)


def guess_fileformat(file):
    JSON_INDICATORS = ['{', '[', ']', '}']
    HTML_INDICATORS = ['<', '>']
    i = 0
    while file[i] not in JSON_INDICATORS + HTML_INDICATORS and i < len(file):
        i += 1

    return Format.HTML if file[i] in HTML_INDICATORS else (Format.JSON if file[i] in JSON_INDICATORS else DEFAULT_FORMAT)


def set_fileformat(filename, fformat):
    if fformat:
        return Format(fformat)
    else:
        ext = filename.split('.')[-1]
        return Format(ext) if ext in VALID_FORMATS else DEFAULT_FORMAT


def main():
    ff_package_name = "org.mozilla.firefox"
    infile = None
    outfile = None
    fileformat = None
    copydb = False
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
                        help='Specify the output format. If omitted, the format is determined from the outfile extension or infile contents or JSON as fallback.')
    parser.add_argument('-d',
                        '--db-file',
                        type=str,
                        action='store',
                        dest='dbfile',
                        required=('-c' in sys.argv or '--copy' in sys.argv),
                        default=dbfile,
                        help=f'Use DBFILE instead of fetching the places.sqlite db from the device')
    parser.add_argument('--public-key',
                        type=str,
                        action='store',
                        dest='pubkey',
                        required=False,
                        default=pubkey,
                        help=f'The public key file to use. Defaults to "{pubkey}".')
    parser.add_argument('--private-key',
                        type=str,
                        action='store',
                        dest='privkey',
                        required=False,
                        default=privkey,
                        help=f'The private key file to use. Defaults to "{privkey}".')
    command_group = parser.add_mutually_exclusive_group(required=True)
    command_group.add_argument('-i',
                               '--import',
                               type=str,
                               action='store',
                               nargs='?',
                               const='',
                               dest='infile',
                               default=infile,
                               help='Import the booksmarks from INFILE. If INFILE is omitted stdin is used')
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
    ff_package_name = args.ff_package_name
    infile = args.infile
    outfile = args.outfile
    fileformat = set_fileformat(infile or outfile or '', args.fformat)
    copydb = args.copydb
    dbfile = args.dbfile
    pubkey = args.pubkey
    privkey = args.privkey

    if copydb or dbfile is None:
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
        device.shell(f'su -c \'{get_db_commands}\'')

        if dbfile is None:
            tmpdir = tempfile.TemporaryDirectory()
            dbfile = os.path.join(tmpdir.name, DB_FILE_NAME)

        # Copy db to host
        device.pull(DB_TEMP_FILE, dbfile)

        # Cleanup
        device.shell(f'rm -f \'{DB_TEMP_FILE}\'')

    with sqlite3.connect(dbfile) as conn:
        with closing(conn.cursor()) as cur:

            # The export flag has been used
            if outfile is not None:
                # Get the root node and build a bookmarks object
                with closing(cur.execute(BASE_QUERY, (ROOT[0],))) as res:
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
                    print(bookmarks_out)
                else:
                    with open(outfile, 'w') as ofile:
                        ofile.write(bookmarks_out)

            # The import flag has been used
            elif infile is not None:
                if infile == '':
                    file_contents = sys.stdin.read()
                else:
                    with open(infile, 'r') as ifile:
                        file_contents = ifile.read()

                if fileformat is None:
                    fileformat = guess_fileformat(file_contents)

                if fileformat == Format.HTML:
                    with closing(cur.execute(NODE_QUERY, (ROOT[0],))) as res:
                        bookmarks_root = export_node(None, res.fetchone())

                    with closing(cur.execute(NODE_QUERY, (ROOT[1],))) as res:
                        bookmarks_menu = export_node(None, res.fetchone())

                    bookmarks_root['children'] = [bookmarks_menu]
                    bookmarks = html_to_bookmarks(file_contents, bookmarks_root, max_id)
                else:
                    # Use JSON as default fallback
                    bookmarks = json.loads(file_contents)

                import_node(conn, bookmarks)

                # Copy db to device


            # This should not happen
            elif not copydb:
                raise RuntimeError('No export or import or copydb flag given.')


if __name__ == '__main__':
    main()
