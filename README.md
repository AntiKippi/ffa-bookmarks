# ffa-bookmarks
ffa-bookmarks is a flexible command line utility to manage your [Firefox for Android](https://www.mozilla.org/en-US/firefox/browsers/mobile/android/) bookmarks without going through the [cloud](https://www.mozilla.org/en-US/firefox/features/sync/).
It features exporting or importing your bookmarks to or from an HTML or JSON file.

## Requirements
 - Python v3.6 or higher
 - Some basic confidence in working with the terminal
 - An Android Phone with root access and ADB Debugging enabled

## Installation
Installation is performed via git by cloning this repository. 
Then you have to ensure that you have installed all the required packages listed in `requirements.txt`.

```
$ git clone https://github.com/AntiKippi/ffa-bookmarks.git
$ cd ffa-bookmarks
$ pip install -r requirements.txt
```

After that the `ffa-bmk.py` script is ready to use.

## Usage

The basic workflow is:
 - Connecting your device via USB or network
 - Execute `ffa-bmk.py` with the desired command line arguments
 - Check your device and grant all ADB and root requests.

### Exporting
Exporting is done with the `-e` switch. The resulting output is compatible with Firefox Desktop and can be imported there.

Note that to show the "Mobile Bookmarks" folder on Firefox Desktop you need to change `browser.bookmarks.showMobileBookmarks` in about:config to true (if it does not exist, create it).

### Importing
Importing is done with the `-i` switch. Any file created by either Firefox Desktop or exported with ffa-bookmarks can be used.

Note that if the input is in JSON format, existing bookmarks might get overwritten, so have some caution here.

### Examples
For a full description of all available options see `ffa-bmk.py -h`.

#### Export bookmarks to `/path/to/bmk.json`
`$ ffa-bmk.py -e /path/to/bmk.json`

#### Export all bookmarks from an existing places.sqlite file at `/path/to/places.sqlite` to stdout in HTML format
`$ ffa-bmk.py -d /path/to/places.sqlite -e -f html`

#### Import bookmarks from `/path/to/file` in HTML format
`$ ffa-bmk.py -i /path/to/file -f html`

#### Import bookmarks from `/path/to/bmk.json` to [Mull](https://f-droid.org/packages/us.spotco.fennec_dos/):
`$ ffa-bmk.py -i /path/to/bmk.json -p us.spotco.fennec_dos`

## Bugs
If you find a bug please create an issue in this repository.

# Donations
I currently don't accept donations. However, if you find my work useful and want to say "Thank you!" consider starring this repository ⭐.