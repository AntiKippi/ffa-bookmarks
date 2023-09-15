#!/usr/bin/env python3

######################################################
# ffa-bookmarks
# Utility to manage bookmarks in Firefox for Android
#
# Author: Kippi
# Version: 0.0.0
######################################################

import argparse
import os
import sqlite3
import tempfile
from adb_shell.adb_device import AdbDeviceTcp, AdbDeviceUsb
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from enum import StrEnum, auto


class Format(StrEnum):
    HTML = auto(),
    JSON = auto()

VALID_FORMATS = [f.value for f in Format]
DEFAULT_FORMAT = Format.HTML
DB_FILE_NAME = 'places.sqlite'
ANDROID_TEMP_DIR = '/data/local/tmp'
DB_TEMP_FILE = f'{ANDROID_TEMP_DIR}/{DB_FILE_NAME}'


def set_fileformat(filename, fformat):
    if fformat is None:
        ext = filename.split('.')[-1]
        return Format(ext) if ext in VALID_FORMATS else DEFAULT_FORMAT
    else:
        return Format(fformat)


def main():
    ff_package_name = "org.mozilla.firefox"
    infile = None
    outfile = None
    fileformat = None
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
    infile = args.infile
    outfile = args.outfile
    fileformat = set_fileformat(infile or outfile, args.fformat)
    pubkey = args.pubkey
    privkey = args.privkey


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
    conn = sqlite3.connect(dbfile)
    cur = conn.cursor()

    # The export flag has been used
    if outfile is not None:
        print(dbfile)
    # The import flag has been used
    elif infile is not None:
        print(dbfile)
    # This should not happen
    else:
        raise RuntimeError('No export or import flag given.')



if __name__ == '__main__':
    main()
