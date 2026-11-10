"""Заглушки служб iPhone для тестов: файловая система в памяти и «подключённое устройство»."""

from datetime import datetime

DIRECTORY = "S_IFDIR"
REGULAR = "S_IFREG"
SYMLINK = "S_IFLNK"
MODIFIED_AT = datetime(2024, 5, 17, 9, 30)


class FakeAfc:
    """directories: {путь: [имена]}; files: {путь: размер в байтах}. Содержимое файла — байты «x»."""

    def __init__(self, directories, files, symlinks=(), broken=()):
        self._directories = directories
        self._files = files
        self._symlinks = set(symlinks)
        self._broken = set(broken)
        self._handles = {}
        self.open_handles = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def listdir(self, path):
        if path in self._broken or path not in self._directories:
            raise OSError(f"cannot list {path}")
        return list(self._directories[path])

    async def stat(self, path):
        if path in self._broken:
            raise OSError(f"cannot stat {path}")
        if path in self._symlinks:
            return {"st_ifmt": SYMLINK, "st_size": 10, "st_mtime": MODIFIED_AT}
        if path in self._directories:
            return {"st_ifmt": DIRECTORY, "st_size": 0, "st_mtime": MODIFIED_AT}
        return {"st_ifmt": REGULAR, "st_size": self._files[path], "st_mtime": MODIFIED_AT}

    async def get_size_of_path_contents(self, path):
        prefix = path.rstrip("/") + "/"
        return {"st_size": sum(size for name, size in self._files.items() if name.startswith(prefix))}

    async def fopen(self, path, mode="r"):
        if path not in self._files:
            raise OSError(f"cannot open {path}")
        handle = len(self._handles) + 1
        self._handles[handle] = self._files[path]
        self.open_handles += 1
        return handle

    async def fread(self, handle, size):
        count = min(size, self._handles[handle])
        self._handles[handle] -= count
        return b"x" * count

    async def fclose(self, handle):
        self.open_handles -= 1


class FakeLockdown:
    def __init__(self, disk_usage=None):
        self.all_values = {"DeviceName": "Тестовый iPhone", "ProductType": "iPhone15,2", "ProductVersion": "18.1"}
        self.udid = "FAKE-UDID"
        self._disk_usage = disk_usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def get_value(self, domain=None, key=None):
        return self._disk_usage


class RecordingProgress:
    """Запоминает всё, что операция сообщила о ходе работы."""

    def __init__(self):
        self.total = None
        self.bytes = 0
        self.percents = []
        self.destination = None

    def set_total(self, total_bytes):
        self.total = total_bytes

    def add_bytes(self, count, current):
        self.bytes += count

    def set_percent(self, percent, current):
        self.percents.append(percent)

    def set_destination(self, destination):
        self.destination = destination
