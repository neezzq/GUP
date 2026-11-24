---
name: python-desktop-ui
description: Build and check the Game Updater CustomTkinter window.
---
Keep UI writes on the Tk thread. Return typed events from background work through a queue
and drain it with after(). Test RU and EN, DPI 100% and 150%, and indeterminate download states.
Verify pause blocks new work while the client can complete active disk operations.
Reference: https://github.com/TomSchimansky/CustomTkinter
