---
name: windows-build-testing
description: Build the x64 EXE and verify the packaged Windows application.
---
Use Python 3.12 x64 and client/build.bat on Windows. Run GameUpdater.exe --smoke-test and verify
the SHA-256 artifact. Test enrollment and a real update per launcher on Windows 10 or 11 before
claiming integration support. Record automated queue tests separately from real launcher results.
Reference: https://pyinstaller.org/en/stable/
