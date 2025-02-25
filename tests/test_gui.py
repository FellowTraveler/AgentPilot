import sys
import time
import unittest

from PySide6.QtGui import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from src.gui.main import Main
from src.gui.widgets import IconButton


# TEST LIST
# - Open app with no internet/
# - Open convos page
# -   Double click context/
# -   Chat button/
# -   Delete button/
# -   Search bar/
# -   New folder/
# -   Right click context items/

# - Open agent page
# -   New agent button/
# -   Double click agent/
# -   Chat button/
# -   New folder/
# -   Delete button/
# -     Info tab settings
# -       Change plugin
# -       Change avatar & name
# -     Chat tab settings
# -       Message tab settings
# -       Preload tab settings
# -       Group tab settings
# -     Tools tab settings
# -     Voice tab settings

# - Open settings page
# -   System tab settings
# -     Dev mode
# -     Telemetry
# -     Always on top
# -     Default model
# -     Auto title
# -   Display tab settings
# -   Model tab settings
# -     Edit api
# -     New model
# -     Delete model
# -     Edit model
# -   Blocks tab settings
# -   Sandbox tab settings

# - Chat page
# -   Edit title
# -   Navigation buttons
# -   Openai agent
# -   Perplexity agent
# -   Multi agent mixed providers
# -   Context placeholders
# -   Hide responses
# -   Decoupled scroll
# -   Stop button

# - Plugins
# -   Open interpreter
# -   Openai assistant
# -   Memgpt
# -   Autogen agents (with and without context plugin)
# -   Autogen context plugin
#

# - Other
# -   Push/pull buttons


class TestYourApp(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication(sys.argv)
        cls.main = Main()

    @classmethod
    def tearDownClass(cls):
        # Clean up after all tests
        cls.main.close()
        cls.app.quit()

    def setUp(self):
        self.main.show()
        self.main.raise_()
        QTest.qWait(1000)  # Wait for the window to show

        # self.btn_contexts = self.main.main_menu.settings_sidebar.page_buttons['Contexts']
        # self.btn_agents = self.main.main_menu.settings_sidebar.page_buttons['Agents']
        self.btn_settings = self.main.main_menu.settings_sidebar.page_buttons['Settings']

        # self.page_contexts = self.main.main_menu.pages['Contexts']
        # self.page_agents = self.main.main_menu.pages['Agents']
        self.page_settings = self.main.main_menu.pages['Settings']

    def goto_page(self, page_name):
        btn = self.main.main_menu.settings_sidebar.page_buttons.get(page_name, None)
        page = self.main.main_menu.pages.get(page_name, None)
        if btn:
            if not btn.isVisible():
                btn = None
                page = None
        if not btn:
            btn = self.page_settings.settings_sidebar.page_buttons.get(page_name, None)
            page = self.page_settings.settings_sidebar.pages.get(page_name, None)
            if not btn.isVisible():
                btn = None
                page = None
        if not btn:
            raise ValueError(f'Page {page_name} not found')
        QTest.mouseClick(btn, Qt.LeftButton)
        QTest.qWait(500)

        return page

    def iterate_button_bar(self, button_bar):
        def do_btn_add(button):
            QTest.mouseClick(button, Qt.LeftButton)
            QTest.qWait(500)

        def do_btn_del(button):
            QTest.mouseClick(button, Qt.LeftButton)
            QTest.qWait(500)

        for attr_name, obj in button_bar.__dict__.items():
            if not isinstance(obj, IconButton) or not obj.isVisible() or not obj.isEnabled():
                continue
            print(attr_name)
            if attr_name == 'btn_add':
                do_btn_add(obj)
            elif attr_name == 'btn_del':
                do_btn_del(obj)

    def test_chat_page(self):
        page_contexts = self.goto_page('Contexts')
        self.iterate_button_bar(page_contexts.tree_buttons)

        #
        # # Check UI state
        # # For example, check if a label text has changed
        # label = self.window.findChild(QLabel, "yourLabelName")
        # self.assertEqual(label.text(), "Expected Text")
        #
        # # Or check if a widget is visible
        # widget = self.window.findChild(QWidget, "someWidgetName")
        # self.assertTrue(widget.isVisible())
        # endregion


if __name__ == '__main__':
    unittest.main()