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
