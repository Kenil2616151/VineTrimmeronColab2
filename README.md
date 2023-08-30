# vinetrimmer

Widevine DRM downloader and decrypter

## Requirements

* [Python](https://python.org/) 3.7 or newer
* [Poetry](https://python-poetry.org/)
* Python package dependencies

## Binaries

* [CCExtractor](https://ccextractor.org/)
* [FFmpeg](https://ffmpeg.org/ffmpeg.html) (ffmpeg and ffprobe)
* [MKVToolNix](https://mkvtoolnix.download/) v50 or newer (mkvmerge)
* [Shaka Packager](https://github.com/google/shaka-packager) (packager) or [Bento4](https://github.com/truedread/bento4) (mp4decrypt)

## Installation

1. Install the requirements above, place the binaries in your PATH.
2. Clone the GitHub repo or download a zip of it
3. Run `poetry config virtualenvs.in-project true` (optional but recommended)
4. Run `poetry install`
5. You can now do `poetry shell` to activate the virtual environment and then use the `vt` command.

## Configuration

Example configuration files are available in the `example_configs` directory.
These should be copied into the appropriate directory for your platform.

* Windows: `%LOCALAPPDATA%\vinetrimmer`
* macOS: `~/Library/Preferences/vinetrimmer`
* Linux: `~/.config/vinetrimmer`

After that, edit the files as appropriate to configure the tool.

## Data

The data directory contains other non-configuration data required to use the tool.
The appropriate directory for each platform:

* Windows: `%LOCALAPPDATA%\vinetrimmer`
* macOS: `~/Library/Application Support/vinetrimmer`
* Linux: `~/.local/share/vinetrimmer`

You will need a CDM to be able to decrypt content from services (unless you use unencrypted services or cached keys).
These should be placed in the `devices` directory in `.wvd` format or as a directory containing `device_private_key`,
`device_client_id_blob`, `device_vmp_blob` (optional) and `wv.json`.

Some services may also require cookies instead of (or in addition to) credentials.
Place them in `cookies/<service>/<profile>.txt` (the service name should be properly capitalized as shown in `vt dl -h`,
and the profile name should match the one in the config).
