LAUNCHERS = {
    "steam": "Steam",
    "epic": "Epic Games Store",
    "riot": "Riot Client",
    "battlenet": "Battle.net",
    "vkplay": "VK Play",
    "ea": "EA App",
    "lesta": "Lesta Game Center",
}


def adapters():
    from .battlenet import BattleNet
    from .ea import EA
    from .epic import Epic
    from .lesta import Lesta
    from .riot import Riot
    from .steam import Steam
    from .vkplay import VKPlay

    return {cls.key: cls for cls in (Steam, Epic, Riot, BattleNet, VKPlay, EA, Lesta)}


def scan_launchers(paths, launcher_types=None):
    from client.automation import installed_programs, running_programs

    programs = tuple(installed_programs())
    running = tuple(exe for _, exe in running_programs())
    for key, launcher_type in (adapters() if launcher_types is None else launcher_types).items():
        try:
            launcher = launcher_type(paths.get(key, ""), programs)
            launcher.running_exes = running
            if launcher.detect():
                yield key, launcher, "installed"
            else:
                yield key, None, "signature" if launcher.locked else "not_installed"
        except Exception:
            yield key, None, "detect_failed"
