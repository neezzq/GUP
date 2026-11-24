import re

from .base import Launcher


class BattleNet(Launcher):
    key = "battlenet"
    login_hosts = ('account.battle.net', 'account.blizzard.com', 'oauth.battle.net', 'us.battle.net', 'eu.battle.net')
    display_names = ("battle.net", "battle.net app")
    publishers = ("Blizzard Entertainment, Inc.", "Blizzard Entertainment, Inc", "Blizzard Entertainment")
    suffixes = ("Battle.net.exe", "Battle.net Launcher.exe")
    paths = (
        ("ProgramFiles(x86)", "Battle.net/Battle.net.exe"),
        ("ProgramFiles", "Battle.net/Battle.net.exe"),
    )

    def matches_process(self, exe):
        return super().matches_process(exe) or (
            exe.name.casefold() == "battle.net.exe"
            and exe.parent.parent == self.exe.parent
            and re.fullmatch(r"Battle\.net\.[0-9]+", exe.parent.name, re.IGNORECASE) is not None
        )

    def discover_games(self):
        def installed_by_agent(program):
            return bool(re.search(r"battle\.net.*?\s--uid=[^\s]+", program.uninstall, re.IGNORECASE))

        return self.registry_games(self.publishers, installed_by_agent)
