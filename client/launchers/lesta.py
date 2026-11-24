from .base import Launcher


class Lesta(Launcher):
    key = "lesta"
    login_hosts = ('lesta.ru', 'id.lesta.ru')
    display_names = ("lesta game center", "леста гейм центр", "игровой центр леста")
    publishers = ("Lesta Games LLC", "Lesta Games", "LESTA LLC", "Lesta Studio LLC", "ООО «Леста»")
    suffixes = ("lgc.exe", "GameCenter.exe")
    paths = (
        ("ProgramData", "Lesta Game Center/lgc.exe"),
        ("ProgramFiles(x86)", "Lesta Game Center/lgc.exe"),
        ("ProgramFiles", "Lesta Game Center/lgc.exe"),
    )
