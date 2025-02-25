from src.system.apis import APIManager
from src.system.config import ConfigManager
from src.system.blocks import BlockManager
from src.system.files import FileManager
from src.system.modules import ModuleManager
from src.system.providers import ProviderManager
from src.system.roles import RoleManager
from src.system.environments import EnvironmentManager
from src.system.plugins import PluginManager
from src.system.tools import ToolManager
from src.system.vectordbs import VectorDBManager
from src.system.venvs import VenvManager
from src.system.workspaces import WorkspaceManager


class SystemManager:
    def __init__(self):
        self.apis = APIManager()
        self.blocks = BlockManager(parent=self)
        self.config = ConfigManager()
        # self.files = FileManager()
        self.providers = ProviderManager(parent=self)
        self.plugins = PluginManager()
        self.modules = ModuleManager(parent=self)
        self.roles = RoleManager()
        self.environments = EnvironmentManager()
        self.tools = ToolManager(parent=self)
        self.vectordbs = VectorDBManager(parent=self)
        self.venvs = VenvManager(parent=self)
        self.workspaces = WorkspaceManager(parent=self)

    def load(self):
        initial_items = list(self.__dict__.values())  # todo dirty
        for mgr in initial_items:  # self.__dict__.values():
            if hasattr(mgr, 'load'):
                mgr.load()
        for mgr in self.__dict__.values():
            if mgr not in initial_items and hasattr(mgr, 'load'):
                mgr.load()


manager = SystemManager()
