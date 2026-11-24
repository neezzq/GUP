from .base import Launcher


class EA(Launcher):
    key = "ea"
    login_hosts = ('signin.ea.com', 'accounts.ea.com')
    display_names = ("ea app", "ea desktop")
    publishers = ("Electronic Arts Inc.", "Electronic Arts, Inc.", "Electronic Arts Inc")
    suffixes = ("EADesktop.exe", "EA Desktop/EADesktop.exe")
    paths = (
        ("ProgramFiles", "Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe"),
        ("ProgramFiles(x86)", "Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe"),
    )
    helper_paths = ("EACefSubProcess.exe",)

    def discover_games(self):
        excluded = {"ea anticheat", "ea javelin anticheat"}
        return (game for game in self.registry_games(self.publishers) if game.name.casefold() not in excluded)
