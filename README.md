# TidyPaper for Windows and Mac

A plain-English PDF and document helper: read, edit, sign, fill in, scan, redact and convert.
This is the source of **TidyPaper 0.2.17 for Mac and for Windows** (version 0.2.17).

- The app and downloads: https://tidypaper.io
- Licence: GNU Affero General Public License, version 3 - see [LICENSE](LICENSE) and [COPYRIGHT.md](COPYRIGHT.md)
- Other people's work in it: [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt)
- Where their source is: [THIRD-PARTY-SOURCES.md](THIRD-PARTY-SOURCES.md)

## Run it from source (Windows, Python 3.13)

    py -m pip install -r requirements.txt -c versions-windows.txt
    py app.py

## Build it

Windows (Inno Setup 6 is needed for the installer; without it you get the app folder and a zip):

    build\build_installer.bat nopause

Mac (Apple Silicon; Homebrew's python@3.12, tesseract and ffmpeg):

    build/build_mac.sh nonotary

`versions-windows.txt` has the versions the Windows release was built with. The app has no signing keys in it and
needs none to build. You can install and run what you build in place of the published app.

## What isn't here

- The iPhone, iPad and Android apps, which are separate programs.
- The optional "Shared AI" service. It is a separate program the app talks to over the internet; the app also works
  without it, with your own key for an AI service, or with Ollama on your own computer.
- The project's tests, which use files from those private projects.

## If you share a changed copy

Give it another name (`brand.json`) and set `update_url` to your own address or to "". Left as it is, your copy
would replace itself with the official TidyPaper at the next update.

## Problems and security

Write to support@tekvibes.com. Please report a security problem there privately rather than in a public issue.
Privacy: https://tidypaper.io/privacy
