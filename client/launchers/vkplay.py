from .base import Launcher


class VKPlay(Launcher):
    key = "vkplay"
    login_hosts = ('id.vk.com', 'vkplay.ru')
    display_names = ("vk play", "vk play gamecenter", "игровой центр vk play", "игровой центр mail.ru")
    publishers = ("VK LLC", "V Kontakte LLC", "V Kontakte, LLC", "Mail.Ru LLC", "Mail.Ru Ltd.", "MY.GAMES B.V.")
    suffixes = ("GameCenter.exe",)
    paths = (
        ("LOCALAPPDATA", "GameCenter/GameCenter.exe"),
        ("ProgramFiles(x86)", "VK Play/GameCenter.exe"),
        ("ProgramFiles", "VK Play/GameCenter.exe"),
    )
