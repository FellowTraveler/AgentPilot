import asyncio
import json
import sqlite3
import uuid
from abc import abstractmethod
from functools import partial

from src.gui.windows.workspace import WorkspaceWindow
from src.members.base import Member
from src.members.agent import Agent
from src.members.block import TextBlock
from src.members.node import Node
from src.members.user import User, UserSettings

from src.utils import sql
from src.utils.messages import MessageHistory

from PySide6.QtCore import QPointF, QRectF, QPoint, Signal, QTimer
from PySide6.QtGui import Qt, QPen, QColor, QBrush, QPainter, QPainterPath, QCursor, QRadialGradient, \
    QPainterPathStroker, QPolygonF
from PySide6.QtWidgets import QWidget, QGraphicsScene, QGraphicsEllipseItem, QGraphicsItem, QGraphicsView, \
    QMessageBox, QGraphicsPathItem, QStackedLayout, QMenu, QInputDialog, QGraphicsWidget, \
    QSizePolicy, QApplication, QFrame, QTreeWidgetItem, QSplitter, QVBoxLayout

from src.gui.config import ConfigWidget, CVBoxLayout, CHBoxLayout, ConfigFields, ConfigPlugin, IconButtonCollection, \
    ConfigJsonTree, ConfigJoined

from src.gui.widgets import IconButton, ToggleIconButton, TreeDialog, BaseTreeWidget, find_main_widget
from src.utils.helpers import path_to_pixmap, display_messagebox, get_avatar_paths_from_config, \
    merge_config_into_workflow_config, get_member_name_from_config, block_signals

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


class Workflow(Member):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        from src.system.base import manager

        self._parent_workflow = kwargs.get('workflow', None)
        self.system = manager
        self.member_type = 'workflow'
        self.config = kwargs.get('config', None)
        self.params = kwargs.get('params', None) or {}  # optional, usually only used for tool / block workflows
        self.tool_uuid = kwargs.get('tool_uuid', None)  # only used for tool workflows

        if self._parent_workflow is None:
            self.context_id = kwargs.get('context_id', None)
            get_latest = kwargs.get('get_latest', False)
            kind = kwargs.get('kind', 'CHAT')  # throwaway for now, need to try to keep it that way

            if get_latest and self.context_id is not None:
                print("Warning: get_latest and context_id are both set, get_latest will be ignored.")  # todo warnings
            if get_latest and self.context_id is None:
                # Load latest context
                self.context_id = sql.get_scalar("SELECT id FROM contexts WHERE parent_id IS NULL AND kind = ? ORDER BY id DESC LIMIT 1",
                                                 (kind,))

            if self.context_id is not None:
                if self.config:
                    print("Warning: config is set, but will be ignored because an existing workflow is being loaded.")  # todo warnings
                config_str = sql.get_scalar("SELECT config FROM contexts WHERE id = ?", (self.context_id,))
                self.config = json.loads(config_str) or {}

            else:
                # # Create new context
                kind_init_members = {
                    'CHAT': 'agent',
                    'BLOCK': 'block',
                }
                if not self.config:
                    init_member_config = {'_TYPE': kind_init_members[kind]}
                    self.config = merge_config_into_workflow_config(init_member_config)
                sql.execute("INSERT INTO contexts (kind, config) VALUES (?, ?)", (kind, json.dumps(self.config),))
                self.context_id = sql.get_scalar("SELECT id FROM contexts WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))

        self.loop = asyncio.get_event_loop()
        self.responding = False
        self.stop_requested = False

        self.members = {}  # id: member
        self.boxes = []  # list of lists of member ids

        self.autorun = True
        self.behaviour = None
        self.gen_members = []

        # self.config = kwargs.get('config', None)
        # if self.config is None:
        #     self.load_config()

        # Load base workflow
        if not self._parent_workflow:
            # self._context_id
            self._chat_name = ''
            self._chat_title = ''
            self._leaf_id = self.context_id
            self._message_history = MessageHistory(self)

        self.load()
        self.receivable_function = self.behaviour.receive

    @property
    def context_id(self):
        return self.get_from_root('_context_id')

    @property
    def chat_name(self):
        return self.get_from_root('_chat_name')

    @property
    def chat_title(self):
        return self.get_from_root('_chat_title')

    @property
    def leaf_id(self):
        return self.get_from_root('_leaf_id')

    @property
    def message_history(self):
        return self.get_from_root('_message_history')

    @context_id.setter
    def context_id(self, value):
        self._context_id = value

    @chat_name.setter
    def chat_name(self, value):
        self._chat_name = value

    @chat_title.setter
    def chat_title(self, value):
        self._chat_title = value

    @leaf_id.setter
    def leaf_id(self, value):
        self._leaf_id = value

    @message_history.setter
    def message_history(self, value):
        self._message_history = value

    def get_from_root(self, attr_name):
        if hasattr(self, attr_name):
            return getattr(self, attr_name, None)
        return self._parent_workflow.get_from_root(attr_name)

    def load_config(self, json_config=None):
        if json_config is None:
            if self._parent_workflow:
                member_config = self._parent_workflow.get_member_config(self.member_id)
                self.config = member_config
            else:
                config_str = sql.get_scalar("SELECT config FROM contexts WHERE id = ?", (self.context_id,))
                self.config = json.loads(config_str) or {}
        else:
            if isinstance(json_config, str):
                json_config = json.loads(json_config)
            self.config = json_config

    def load(self):
        workflow_config = self.config.get('config', {})
        self.autorun = workflow_config.get('autorun', True)
        self.load_members()

        if self._parent_workflow is None:
            # Load base workflow
            self.message_history.load()
            self.chat_title = sql.get_scalar("SELECT name FROM contexts WHERE id = ?", (self.context_id,))

    def load_members(self):
        from src.system.plugins import get_plugin_class
        # Get members and inputs from the loaded json config
        if self.config.get('_TYPE', 'agent') == 'workflow':  # !! #
            members = self.config['members']
        else:  # is a single entity, this allows single entity to be in workflow config for simplicity
            wf_config = merge_config_into_workflow_config(self.config)
            members = wf_config.get('members', [])
        inputs = self.config.get('inputs', [])

        last_member_id = None
        last_loc_x = -100
        current_box_member_ids = set()

        members = sorted(members, key=lambda x: x['loc_x'])

        self.members = {}  #!looper!#
        self.boxes = []
        iterable = iter(members)
        while len(members) > 0:
            try:
                member_dict = next(iterable)
            except StopIteration:  # todo temp make nicer
                iterable = iter(members)
                continue

            member_id = str(member_dict['id'])
            if self._parent_workflow:
                pass
            entity_id = member_dict['agent_id']
            member_config = member_dict['config']
            loc_x = member_dict.get('loc_x', 50)
            loc_y = member_dict.get('loc_y', 0)

            member_input_ids = [
                str(input_info['input_member_id'])
                for input_info in inputs
                if str(input_info['member_id']) == str(member_id)  # todo
                and not input_info['config'].get('looper', False)
            ]

            # Order based on the inputs
            if len(member_input_ids) > 0:
                if not all((inp_id in self.members) for inp_id in member_input_ids):
                    continue

            # Instantiate the member
            member_type = member_dict.get('config', {}).get('_TYPE', 'agent')
            kwargs = dict(main=self.main,
                          workflow=self,
                          member_id=str(member_id),
                          config=member_config,
                          agent_id=entity_id,
                          loc_x=loc_x,
                          loc_y=loc_y,
                          inputs=member_input_ids)
            if member_type == 'agent':
                use_plugin = member_config.get('info.use_plugin', None)
                member = get_plugin_class('Agent', use_plugin, kwargs) or Agent(**kwargs)
            elif member_type == 'workflow':
                member = Workflow(**kwargs)
            elif member_type == 'user':
                member = User(**kwargs)
            elif member_type == 'block':
                use_plugin = member_config.get('block_type', None)
                member = get_plugin_class('Block', use_plugin, kwargs) or TextBlock
                member = member(**kwargs)  # todo we need to instantiate the class here for now
            elif member_type == 'node':
                member = Node(**kwargs)
            else:
                raise NotImplementedError(f"Member type '{member_type}' not implemented")

            member.load()

            if member_type in ('workflow', 'agent', 'block'):
                if abs(loc_x - last_loc_x) < 10:  # 10px threshold
                    if last_member_id is not None:
                        current_box_member_ids |= {last_member_id}
                    current_box_member_ids |= {member_id}

                else:
                    if current_box_member_ids:
                        self.boxes.append(current_box_member_ids)
                        current_box_member_ids = set()

                last_loc_x = loc_x
                last_member_id = member_id

            self.members[member_id] = member
            members.remove(member_dict)
            iterable = iter(members)

        if current_box_member_ids:
            self.boxes.append(current_box_member_ids)

        del_boxes = []
        for box in self.boxes:
            for member_id in box:
                fnd = self.walk_inputs_recursive(member_id, box)
                if fnd:
                    del_boxes.append(box)
                    break
        for box in del_boxes:
            self.boxes.remove(box)

        counted_members = self.count_members()
        if counted_members == 1:
            other_members = self.get_members(excl_types=('user',))
            self.chat_name = next(iter(other_members)).config.get('info.name', 'Assistant')
        else:
            self.chat_name = f'{counted_members} members'

        self.update_behaviour()

    def walk_inputs_recursive(self, member_id, search_list):  #!asyncrecdupe!# todo clean
        member = self.members[member_id]  #!params!#
        found = False
        for inp in member.inputs:
            # is_looper = self..inputs[inp].config.get('looper', False)
            if inp in search_list:
                return True
            found = found or self.walk_inputs_recursive(inp, search_list)
        return found

    def get_members(self, incl_types='all', excl_types=None):  # ('agent', 'workflow')):
        if incl_types == 'all':
            incl_types = ('agent', 'workflow', 'user', 'tool', 'block', 'node')
        excl_types = excl_types or []
        excl_types = [e for e in excl_types]
        excl_types.append('node')
        incl_types = tuple(t for t in incl_types if t not in excl_types)
        matched_members = [m for m in self.members.values() if m.config.get('_TYPE', 'agent') in incl_types]
        return matched_members

    def count_members(self, incl_types='all', excl_initial_user=True):
        extra_user_count = max(len(self.get_members(incl_types=('user',))) - 1, 0)
        excl_types = ('user',) if excl_initial_user else ()
        matched_members = self.get_members(incl_types=incl_types, excl_types=excl_types)
        return len(matched_members) + (extra_user_count if excl_initial_user else 0)

    def next_expected_member(self):
        """Returns the next member where turn output is None"""
        next_member = next((member for member in self.get_members()
                     if member.turn_output is None),
                    None)
        return next_member

    def next_expected_is_last_member(self):
        """Returns True if the next expected member is the last member"""
        only_one_empty = len([member for member in self.get_members() if member.turn_output is None]) == 1
        return only_one_empty  #!99!#  #!looper!#

    def get_member_async_group(self, member_id):
        for box in self.boxes:
            if member_id in box:
                return box
        return None

    def get_member_config(self, member_id):
        member = self.members.get(member_id)
        return member.config if member else {}

    def reset_last_outputs(self):
        """Reset the last_output and turn_output of all members."""
        for member in self.members.values():
            member.last_output = None
            member.turn_output = None
            if isinstance(member, Workflow):
                member.reset_last_outputs()

    def set_last_outputs(self, map_dict):  # {full_member_id: output}
        for k, v in map_dict.items():
            member = self.get_member_by_full_member_id(k)
            if member:
                member.last_output = v

    def set_turn_outputs(self, map_dict):  # {full_member_id: output}
        for k, v in map_dict.items():
            member = self.get_member_by_full_member_id(k)
            if member:
                member.turn_output = v

    def get_member_by_full_member_id(self, full_member_id):
        """Returns the member object based on the full member id (e.g. '1.2.3')"""
        full_split = str(full_member_id).split('.')  # todo
        workflow = self
        member = None
        for local_id in full_split:
            # if not hasattr(workflow, 'members'):
            #     pass
            member = workflow.members.get(local_id)
            if member is None:
                return None
            workflow = member
        return member

    def save_message(self, role, content, member_id='1', log_obj=None):
        """Saves a message to the database and returns the message_id"""
        if role == 'output':
            content = 'The code executed without any output' if content.strip() == '' else content

        if content == '':
            return None

        new_run = None not in [member.turn_output for member in self.get_members()]  #!looper!#
        if new_run:
            self.message_history.alt_turn_state = 1 - self.message_history.alt_turn_state

        return self.message_history.add(role, content, member_id=member_id, log_obj=log_obj)
        # ^ calls message_history.load_messages after

    def deactivate_all_branches_with_msg(self, msg_id):
        sql.execute("""
            UPDATE contexts
            SET active = 0
            WHERE branch_msg_id = (
                SELECT branch_msg_id
                FROM contexts
                WHERE id = (
                    SELECT context_id
                    FROM contexts_messages
                    WHERE id = ?
                )
            );""", (msg_id,))

    def activate_branch_with_msg(self, msg_id):
        sql.execute("""
            UPDATE contexts
            SET active = 1
            WHERE id = (
                SELECT context_id
                FROM contexts_messages
                WHERE id = ?
            );""", (msg_id,))

    def get_common_group_key(self):
        """Get all distinct group_keys and if there's only one, return it, otherwise return empty key"""
        group_keys = set(getattr(member, 'group_key', '') for member in self.members.values())
        if len(group_keys) == 1:
            return next(iter(group_keys))
        return ''

    def update_behaviour(self):
        """Update the behaviour of the context based on the common key"""
        from src.system.plugins import ALL_PLUGINS
        common_group_key = self.get_common_group_key()
        behaviour = ALL_PLUGINS['Workflow'].get(common_group_key, None)
        self.behaviour = behaviour(self) if behaviour else WorkflowBehaviour(self)

    # async def run_member(self):
    #     """The entry response method for the member."""
    #     # return await self.behaviour.start()
    #     async for key, chunk in self.behaviour.receive():
    #         yield key, chunk

    def get_final_message(self):
        """Returns the final output of the workflow"""
        # last_member = list(self.members.values())[-1]
        # last_member_full_id = last_member.full_member_id()
        # return last_member.last_output
        matched_msgs = self.message_history.get(base_member_id=self.full_member_id())
        return None if not matched_msgs else matched_msgs[-1]


class WorkflowBehaviour:
    def __init__(self, workflow):
        self.workflow = workflow
        self.tasks = []

    async def start(self, from_member_id=None):
        async for key, chunk in self.receive(from_member_id):
            pass

    async def receive(self, from_member_id=None):
        # tasks = []
        self.workflow.gen_members = []
        found_source = False  # todo clean this
        pause_on = ('user', 'contact')
        processed_members = set()

        def create_async_group_task(member_ids):
            """ Helper function to create and return a coroutine that runs all members in the member_async_group """
            async def run_group():
                group_tasks = []
                for member_id in member_ids:
                    if member_id not in processed_members:
                        m = self.workflow.members[member_id]
                        sub_task = asyncio.create_task(run_member_task(m))
                        group_tasks.append(sub_task)
                        processed_members.add(member_id)
                try:
                    await asyncio.gather(*group_tasks)
                except StopIteration:
                    return

            return run_group

        async def run_member_task(member):  # todo dirty
            async for _ in member.run_member():
                pass

        if len(self.workflow.members) == 0:
            return

        # first_member = next(iter(self.workflow.members.values()))
        # if first_member.config.get('_TYPE', 'agent') == 'user':  #!33!#
        #     from_member_id = first_member.member_id

        self.workflow.responding = True
        try:
            for member in self.workflow.members.values():
                if member.member_id == from_member_id or from_member_id is None:
                    found_source = True
                if not found_source:
                    continue
                if member.member_id in processed_members:
                    continue

                filter_role = self.workflow.config.get('config', {}).get('filter_role', 'All').lower()

                async_group_member_ids = self.workflow.get_member_async_group(member.member_id)
                if async_group_member_ids:
                    self.workflow.gen_members = async_group_member_ids
                    # Create a single coroutine to handle the entire member async group
                    run_method = create_async_group_task(async_group_member_ids)
                    result = await run_method()
                    if result is True:
                        return
                else:
                    self.workflow.gen_members = [member.member_id]  # todo
                    # # Run individual member
                    try:
                        async for key, chunk in member.run_member():
                            if key == 'SYS' and chunk == 'SKIP':
                                break

                            nem = self.workflow.next_expected_member()
                            if self.workflow.next_expected_is_last_member() and member == nem:
                                if key == filter_role or filter_role == 'all':
                                    yield key, chunk
                    except StopIteration:
                        return

                if not self.workflow.autorun:
                    return
                if member.config.get('_TYPE', 'agent') in pause_on and member.member_id != from_member_id:
                    return

            if self.workflow._parent_workflow is not None:  # todo
                # last_member = list(self.workflow.members.values())[-1]
                final_message = self.workflow.get_final_message()
                if final_message:
                    full_member_id = self.workflow.full_member_id()
                    log_obj = sql.get_scalar("SELECT log FROM contexts_messages WHERE id = ?", (final_message['id'],))
                    self.workflow.save_message(final_message['role'], final_message['content'], full_member_id, json.loads(log_obj))  # todo switch order & clean double parse json
                    if self.workflow.main:
                        self.workflow.main.new_sentence_signal.emit(final_message['role'], full_member_id, final_message['content'])

        except asyncio.CancelledError:
            pass  # task was cancelled, so we ignore the exception
        except Exception as e:
            raise e
        finally:
            self.workflow.gen_members = []
            self.workflow.responding = False

    def stop(self):
        self.workflow.stop_requested = True
        for member in self.workflow.members.values():
            if member.response_task is not None:
                member.response_task.cancel()


class WorkflowSettings(ConfigWidget):
    def __init__(self, parent, **kwargs):
        super().__init__(parent=parent)
        self.compact_mode = kwargs.get('compact_mode', False)  # For use in agent page
        self.compact_mode_editing = False
        self.db_table = kwargs.get('db_table', None)

        self.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))

        self.members_in_view = {}  # id: member
        self.lines = {}  # (member_id, inp_member_id): line
        self.boxes_in_view = {}  # list of lists of member ids

        self.new_line = None
        self.new_agent = None

        self.autorun = True

        self.layout = CVBoxLayout(self)
        self.workflow_buttons = self.WorkflowButtons(parent=self)

        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 2000, 2000)

        self.view = CustomGraphicsView(self.scene, self)
        # add a border to the view
        # self.view.setStyleSheet("border: 1px solid #000000;")
        # self.view.setFixedHeight(320)

        self.compact_mode_back_button = self.CompactModeBackButton(parent=self)
        self.member_config_widget = DynamicMemberConfigWidget(parent=self)
        self.member_config_widget.hide()  # 32

        h_layout = CHBoxLayout()
        h_layout.addWidget(self.view)

        enable_member_list = self.linked_workflow() is not None
        if enable_member_list:
            self.member_list = self.MemberList(parent=self)
            h_layout.addWidget(self.member_list)
            # self.member_list.tree_members.itemSelectionChanged.connect(self.on_member_list_selection_changed)
            self.member_list.hide()

        self.workflow_config = self.WorkflowConfig(parent=self)
        self.workflow_config.build_schema()
        # h_layout.addWidget(self.workflow_config)

        self.workflow_params = self.WorkflowParams(parent=self)
        self.workflow_params.build_schema()

        self.scene.selectionChanged.connect(self.on_selection_changed)

        # from src.gui.style import TEXT_COLOR
        # self.scene.addLine(0, 0, 0, 2000, QPen(QColor(TEXT_COLOR)))
        # self.scene.addLine(0, 0, 2000, 0, QPen(QColor(TEXT_COLOR)))

        self.workflow_panel = QWidget()
        self.workflow_panel_layout = CVBoxLayout(self.workflow_panel)
        self.workflow_panel_layout.addWidget(self.compact_mode_back_button)
        self.workflow_panel_layout.addWidget(self.workflow_params)
        self.workflow_panel_layout.addWidget(self.workflow_config)
        self.workflow_panel_layout.addWidget(self.workflow_buttons)
        self.workflow_panel_layout.addLayout(h_layout)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.splitter.setChildrenCollapsible(False)

        # self.layout.addWidget(self.workflow_panel)
        # self.layout.addWidget(self.member_config_widget)

        self.splitter.addWidget(self.workflow_panel)
        self.splitter.addWidget(self.member_config_widget)  # , stretch=1)
        self.layout.addWidget(self.splitter)

        # self.view.horizontalScrollBar().setValue(0)
        # self.view.verticalScrollBar().setValue(0)
        # cn = self.__class__.__name__
        # if cn != 'ChatWorkflowSettings':
        #     self.layout.addStretch()

        # self.layout.addStretch()

    def load_config(self, json_config=None):
        if json_config is None:
            json_config = '{}'  # todo
        if isinstance(json_config, str):
            json_config = json.loads(json_config)
        if json_config.get('_TYPE', 'agent') != 'workflow':
            json_config = merge_config_into_workflow_config(json_config)

        json_wf_config = json_config.get('config', {})
        json_wf_params = json_config.get('params', [])
        self.workflow_config.load_config(json_wf_config)
        self.workflow_params.load_config({'data': json_wf_params})  # !55! #
        super().load_config(json_config)

    def get_config(self):
        workflow_config = self.workflow_config.get_config()
        workflow_config['autorun'] = self.workflow_buttons.autorun
        workflow_config['show_hidden_bubbles'] = self.workflow_buttons.show_hidden_bubbles
        workflow_config['show_nested_bubbles'] = self.workflow_buttons.show_nested_bubbles

        workflow_params = self.workflow_params.get_config()

        pass
        config = {
            '_TYPE': 'workflow',
            'members': [],
            'inputs': [],
            'config': workflow_config,
            'params': workflow_params.get('data', []),  # !55! #
        }
        for member_id, member in self.members_in_view.items():
            # # add _TYPE to member_config
            member.member_config['_TYPE'] = member.member_type

            config['members'].append({
                'id': str(member_id),
                'agent_id': None,  # member.agent_id, todo
                'loc_x': int(member.x()),
                'loc_y': int(member.y()),
                'config': member.member_config,
            })
            # todo check if it matters when user member is not first

        for line_key, line in self.lines.items():
            member_id, input_member_id = line_key

            config['inputs'].append({
                'member_id': member_id,
                'input_member_id': input_member_id,
                'config': line.config,
            })

        return config

    def save_config(self):
        """Saves the config to database when modified"""
        if not self.db_table:
            return

        json_config_dict = self.get_config()
        json_config = json.dumps(json_config_dict)

        entity_id = self.parent.get_selected_item_id()
        if not entity_id:
            raise NotImplementedError()  # todo

        try:
            sql.execute(f"UPDATE {self.db_table} SET config = ? WHERE id = ?", (json_config, entity_id))
        except Exception as e:
            display_messagebox(
                icon=QMessageBox.Warning,
                title='Error',
                text='Error saving config:\n' + str(e),
            )

        self.load_config(json_config)  # reload config
        self.load_async_groups()

        for m in self.members_in_view.values():
            m.refresh_avatar()
        if self.linked_workflow() is not None:
            self.linked_workflow().load_config(json_config)
            self.linked_workflow().load()
            self.refresh_member_highlights()
        if hasattr(self, 'member_list'):
            self.member_list.load()
        if hasattr(self.parent, 'on_edited'):
            self.parent.on_edited()

    def load(self):
        self.load_members()
        self.load_inputs()
        self.load_async_groups()
        self.member_config_widget.load()
        self.workflow_params.load()
        self.workflow_config.load()
        self.workflow_buttons.load()

        if hasattr(self, 'member_list'):
            self.member_list.load()

        self.reposition_view()
        self.refresh_member_highlights()

    def load_members(self):
        self.setUpdatesEnabled(False)
        sel_member_ids = [x.id for x in self.scene.selectedItems()
                          if isinstance(x, DraggableMember)]
        # Clear any existing members from the scene
        for m_id, member in self.members_in_view.items():
            self.scene.removeItem(member)
        self.members_in_view = {}

        members_data = self.config.get('members', [])
        # Iterate over the parsed 'members' data and add them to the scene
        for member_info in members_data:
            id = str(member_info['id'])
            # agent_id = member_info.get('agent_id')
            member_config = member_info.get('config')
            loc_x = member_info.get('loc_x')
            loc_y = member_info.get('loc_y')

            member = DraggableMember(self, id, loc_x, loc_y, member_config)
            self.scene.addItem(member)
            self.members_in_view[id] = member

        if self.can_simplify_view():
            self.toggle_view(False)
            # Select the member so that it's config is shown, then hide the workflow panel until more members are added
            other_member_ids = [k for k, m in self.members_in_view.items() if not m.member_config.get('_TYPE', 'agent') == 'user']

            if other_member_ids:
                self.select_ids([other_member_ids[0]])
        else:
            # Show the workflow panel in case it was hidden
            self.toggle_view(True)  # .view.show()
            # # Select the members that were selected before, patch for deselecting members todo
            if not self.compact_mode:
                self.select_ids(sel_member_ids)  # !! #

        self.setUpdatesEnabled(True)

    def load_async_groups(self):
        # Clear any existing members from the scene
        for box in self.boxes_in_view:
            self.scene.removeItem(box)
        self.boxes_in_view = []

        last_member_id = None
        last_member_pos = None
        last_loc_x = -100
        current_box_member_positions = []
        current_box_member_ids = []

        members = self.members_in_view.values()
        members = sorted(members, key=lambda m: m.x())

        for member in members:
            loc_x = member.x()
            loc_y = member.y()
            pos = QPointF(loc_x, loc_y)

            member_type = member.member_config.get('_TYPE', 'agent')
            if member_type in ('workflow', 'agent', 'block'):
                if abs(loc_x - last_loc_x) < 10:
                    current_box_member_positions += [last_member_pos, pos]
                    current_box_member_ids += [last_member_id, member.id]
                else:
                    if current_box_member_positions:
                        box = RoundedRectWidget(self, points=current_box_member_positions, member_ids=current_box_member_ids)
                        self.scene.addItem(box)
                        self.boxes_in_view.append(box)
                        current_box_member_positions = []
                        current_box_member_ids = []

                last_loc_x = loc_x
                last_member_pos = pos
                last_member_id = member.id

        # Handle the last group after finishing the loop
        if current_box_member_positions:
            box = RoundedRectWidget(self, points=current_box_member_positions, member_ids=current_box_member_ids)
            self.scene.addItem(box)
            self.boxes_in_view.append(box)

        del_boxes = []
        for box in self.boxes_in_view:
            # fnd =
            for member_id in box.member_ids:
                fnd = self.walk_inputs_recursive(member_id, box.member_ids)
                if fnd:
                    # self.scene.removeItem(box)
                    # self.boxes_in_view.remove(box)
                    del_boxes.append(box)
                    break

        for box in del_boxes:
            self.scene.removeItem(box)
            self.boxes_in_view.remove(box)

    def walk_inputs_recursive(self, member_id, search_list):  #!asyncrecdupe!# todo clean
        found = False
        member_inputs = [k[1] for k, v in self.lines.items() if k[0] == member_id and v.config.get('looper', False) is False]
        for inp in member_inputs:
            if inp in search_list:
                return True
            found = found or self.walk_inputs_recursive(inp, search_list)
        return found

    def update_member(self, update_list, save=False):
        for member_id, attribute, value in update_list:
            member = self.members_in_view.get(member_id)
            if not member:
                return
            setattr(member, attribute, value)

        if save:
            self.save_config()

    def linked_workflow(self):
        return getattr(self.parent, 'workflow', None)

    def count_other_members(self, exclude_initial_user=True):
        # count members but minus one for the user member
        member_count = len(self.members_in_view)
        if exclude_initial_user and any(m.member_type == 'user' for m in self.members_in_view.values()):
            member_count -= 1
        return member_count

    def can_simplify_view(self):  # !wfdiff! #
        member_count = len(self.members_in_view)
        if member_count == 1:
            member_config = next(iter(self.members_in_view.values())).member_config
            types_to_simplify = ['block']
            if member_config.get('_TYPE', 'agent') in types_to_simplify:
                return True
        elif member_count == 2:
            members = list(self.members_in_view.values())
            members.sort(key=lambda x: x.x())
            first_member = members[0]
            second_member = members[1]
            if first_member.member_type == 'user' and second_member.member_type == 'agent':
                return True
        return False

    def load_inputs(self):
        for _, line in self.lines.items():
            self.scene.removeItem(line)
        self.lines = {}

        inputs_data = self.config.get('inputs', [])
        for input_dict in inputs_data:
            member_id = input_dict['member_id']
            input_member_id = input_dict['input_member_id']
            input_config = input_dict.get('config', {})

            input_member = self.members_in_view.get(input_member_id)
            member = self.members_in_view.get(member_id)

            if input_member is None:  # todo temp
                return
            line = ConnectionLine(self, input_member, member, input_config)
            self.scene.addItem(line)
            self.lines[(member_id, input_member_id)] = line

    def toggle_view(self, visible):
        # self.workflow_panel.setVisible(visible)
        self.view.setVisible(visible)
        # QApplication.processEvents()
        # single shot set sizes
        # self.splitter.setSizes([300 if visible else 21, self.splitter.sizes()[1]])  wrong order?
        # QTimer.singleShot(10, lambda: self.splitter.setSizes([300 if visible else 22, 0 if visible else 1000]))  # 300 if visible else 22, 0]))
        # self.splitter.setSizes([300 if visible else 20, 0 if visible else 1000])
        QTimer.singleShot(10, lambda: self.splitter.setSizes([300 if visible else 22, 0 if visible else 1000]))
        # QTimer.singleShot(11, lambda: partial(self.resizeEvent, None))  # !! #
        self.splitter.setHandleWidth(0 if not visible else 3)

        self.reposition_view()

    def reposition_view(self):
        self.view.horizontalScrollBar().setValue(0)
        self.view.verticalScrollBar().setValue(0)

    def set_edit_mode(self, state):
        if not self.compact_mode:
            return

        self.view.temp_block_move_flag = True

        # deselect all members first, to avoid layout issue - only if multiple other members
        if not self.can_simplify_view() and state is False:
            self.select_ids([])

        # deselecting id's will trigger on_selection_changed, which will hide the member_config_widget
        # and below, when we set the tree to visible, the window resizes to fit the tree
        # so after the member_config_widget is hidden, we need to update geometry
        self.updateGeometry()  # todo check if still needed on all os

        self.compact_mode_editing = state
        if hasattr(self.parent, 'view'):
            self.parent.toggle_view(not state)

        else:
            parent = self.parent
            while not hasattr(parent, 'tree_container'):
                parent = parent.parent
            if hasattr(parent, 'tree_container'):
                parent.tree_container.setVisible(not state)

        self.compact_mode_back_button.setVisible(state)

    def select_ids(self, ids, send_signal=True):
        with block_signals(self.scene):
            for item in self.scene.selectedItems():
                item.setSelected(False)

            for _id in ids:  # todo clean
                if str(_id) in self.members_in_view:
                    self.members_in_view[str(_id)].setSelected(True)
        if send_signal:
            self.on_selection_changed()

    def on_selection_changed(self):
        selected_objects = self.scene.selectedItems()
        selected_agents = [x for x in selected_objects if isinstance(x, DraggableMember)]
        selected_lines = [x for x in selected_objects if isinstance(x, ConnectionLine)]

        can_simplify = self.can_simplify_view()
        if self.compact_mode and not can_simplify and len(selected_objects) > 0 and not self.compact_mode_editing:
            self.set_edit_mode(True)

        # self.setUpdatesEnabled(False)
        if len(selected_objects) == 1:
            if len(selected_agents) == 1:
                member = selected_agents[0]
                self.member_config_widget.display_config_for_member(member)
                self.member_config_widget.show()
                if self.member_config_widget.workflow_settings:
                    self.member_config_widget.workflow_settings.reposition_view()

            elif len(selected_lines) == 1:
                line = selected_lines[0]
                self.member_config_widget.display_config_for_input(line)
                self.member_config_widget.show()

        else:
            self.member_config_widget.hide()  # 32
        # self.setUpdatesEnabled(True)
        if hasattr(self, 'member_list'):
            self.member_list.refresh_selected()

    # def on_member_list_selection_changed(self):
    #     selected_member_ids = [self.member_list.tree_members.get_selected_item_id()]
    #     self.select_ids(selected_member_ids)

    def add_insertable_entity(self, item):
        if self.compact_mode:
            self.set_edit_mode(True) # !position!

        self.toggle_view(True)  # .view.show()
        mouse_scene_point = self.view.mapToScene(self.view.mapFromGlobal(QCursor.pos()))
        if isinstance(item, QTreeWidgetItem):
            item = item.data(0, Qt.UserRole)
        self.new_agent = InsertableMember(
            self,
            json.loads(item['config']),
            mouse_scene_point
        )
        self.scene.addItem(self.new_agent)
        self.view.setFocus()

    def add_entity(self):
        member_in_view_int_keys = [int(k) for k in self.members_in_view.keys()]  # todo clean
        member_id = max(member_in_view_int_keys) + 1 if len(self.members_in_view) else 1
        entity_config = self.new_agent.config
        loc_x, loc_y = self.new_agent.x(), self.new_agent.y()
        member = DraggableMember(self, str(member_id), loc_x, loc_y, entity_config)
        self.scene.addItem(member)
        self.members_in_view[str(member_id)] = member

        self.scene.removeItem(self.new_agent)
        self.new_agent = None

        self.save_config()
        if hasattr(self.parent, 'top_bar'):
            self.parent.load()

        # if not self.compact_mode:
        #     self.parent.reload_selected_item()

    def add_input(self, member_id):
        # if self.new_line.config.get('looper', False):
        #     return
        input_member_id = self.new_line.input_member_id

        if member_id == input_member_id:
            return
        if (member_id, input_member_id) in self.lines:
            return
        cr_check = self.check_for_circular_references(member_id, [input_member_id])
        is_looper = self.new_line.config.get('looper', False)
        if cr_check and not is_looper:
            display_messagebox(
                icon=QMessageBox.Warning,
                title='Warning',
                text='Circular reference detected',
                buttons=QMessageBox.Ok,
            )
            return

        input_member = self.members_in_view[input_member_id]
        member = self.members_in_view[member_id]

        if input_member is None:  # todo temp
            return
        line = ConnectionLine(self, input_member, member, {'input_type': 'Message', 'looper': is_looper})
        self.scene.addItem(line)
        self.lines[(member_id, input_member_id)] = line

        self.scene.removeItem(self.new_line)
        self.new_line = None

        self.save_config()
        # if not self.compact_mode:
        #     self.parent.load()

    def check_for_circular_references(self, member_id, input_member_ids):
        """ Recursive function to check for circular references"""
        connected_input_members = [line_key[1] for line_key, line in self.lines.items()
                                   if line_key[0] in input_member_ids
                                   and line.config.get('looper', False) is False]
        if member_id in connected_input_members:
            return True
        if len(connected_input_members) == 0:
            return False
        return self.check_for_circular_references(member_id, connected_input_members)

    def refresh_member_highlights(self):
        if self.compact_mode or not self.linked_workflow():
            return
        for member in self.members_in_view.values():
            member.highlight_background.hide()

        workflow = self.linked_workflow()
        if len(workflow.gen_members) > 0:
            print('UPDATE MEMBER GENS -------------------')
            # for member_id in workflow.gen_members:
            #     if member_id in self.members_in_view:
            #         self.members_in_view[member_id].highlight_background.show()
        # else:
        next_expected_member = workflow.next_expected_member()
        if not next_expected_member:
            return

        if next_expected_member:
            self.members_in_view[next_expected_member.member_id].highlight_background.show()

    class CompactModeBackButton(QWidget):
        def __init__(self, parent):
            super().__init__(parent)
            self.parent = parent
            self.layout = CHBoxLayout(self)
            self.btn_back = IconButton(
                parent=self,
                icon_path=':/resources/icon-cross.png',
                tooltip='Back',
                size=22,
                text='Close edit mode',
            )
            self.btn_back.clicked.connect(partial(self.parent.set_edit_mode, False))

            self.layout.addWidget(self.btn_back)
            self.layout.addStretch(1)
            self.hide()

    class WorkflowButtons(IconButtonCollection):
        def __init__(self, parent):
            super().__init__(parent=parent)
            self.setFixedHeight(self.icon_size + 4)
            self.layout.addSpacing(15)

            self.autorun = True
            self.show_hidden_bubbles = False
            self.show_nested_bubbles = False

            self.btn_add = IconButton(
                parent=self,
                icon_path=':/resources/icon-new.png',
                tooltip='Add',
                size=self.icon_size,
            )

            self.btn_save_as = IconButton(
                parent=self,
                icon_path=':/resources/icon-save.png',
                tooltip='Save As',
                size=self.icon_size,
            )

            self.btn_clear_chat = IconButton(
                parent=self,
                icon_path=':/resources/icon-clear.png',
                tooltip='Clear Chat',
                size=self.icon_size,
            )

            self.btn_view = ToggleIconButton(
                parent=self,
                icon_path=':/resources/icon-eye.png',
                size=self.icon_size,
            )

            self.btn_add.clicked.connect(self.show_add_context_menu)
            self.btn_save_as.clicked.connect(self.show_save_context_menu)
            self.btn_clear_chat.clicked.connect(self.clear_chat)
            self.btn_view.clicked.connect(self.btn_view_clicked)

            self.layout.addWidget(self.btn_add)
            self.layout.addWidget(self.btn_save_as)
            self.layout.addWidget(self.btn_clear_chat)

            self.layout.addStretch(1)

            self.btn_disable_autorun = ToggleIconButton(
                parent=self,
                icon_path=':/resources/icon-run-solid.png',
                icon_path_checked=':/resources/icon-run.png',
                tooltip='Disable autorun',
                tooltip_when_checked='Enable autorun',
                size=self.icon_size,
            )

            self.btn_member_list = ToggleIconButton(
                parent=self,
                icon_path=':/resources/icon-agent-solid.png',
                tooltip='View member list',
                icon_size_percent=0.9,
                size=self.icon_size,
            )

            self.btn_workflow_params = ToggleIconButton(
                parent=self,
                icon_path=':/resources/icon-parameter.png',
                tooltip='Workflow params',
                size=self.icon_size,
            )

            self.btn_workflow_config = ToggleIconButton(
                parent=self,
                icon_path=':/resources/icon-settings-solid.png',
                tooltip='Workflow config',
                size=self.icon_size,
            )

            self.btn_disable_autorun.clicked.connect(partial(self.toggle_attribute, 'autorun'))
            self.btn_member_list.clicked.connect(self.toggle_member_list)
            self.btn_workflow_params.clicked.connect(self.toggle_workflow_params)
            self.btn_workflow_config.clicked.connect(self.toggle_workflow_config)

            self.layout.addWidget(self.btn_disable_autorun)
            self.layout.addWidget(self.btn_member_list)
            self.layout.addWidget(self.btn_view)
            self.layout.addWidget(self.btn_workflow_params)
            self.layout.addWidget(self.btn_workflow_config)

            self.workflow_is_linked = self.parent.linked_workflow() is not None

            # self.btn_save_as.setVisible(self.workflow_is_linked)
            self.btn_clear_chat.setVisible(self.workflow_is_linked)
            self.btn_view.setVisible(self.workflow_is_linked)
            self.btn_disable_autorun.setVisible(self.workflow_is_linked)
            self.btn_member_list.setVisible(self.workflow_is_linked)
            # self.btn_workflow_config.setVisible(enable_workflow_config)

        def load(self):
            workflow_config = self.parent.config.get('config', {})
            self.autorun = workflow_config.get('autorun', True)
            self.show_hidden_bubbles = workflow_config.get('show_hidden_bubbles', False)
            self.show_nested_bubbles = workflow_config.get('show_nested_bubbles', False)

            self.btn_disable_autorun.setChecked(not self.autorun)
            self.btn_view.setChecked(self.show_hidden_bubbles or self.show_nested_bubbles)

            is_multi_member = self.parent.count_other_members() > 1
            self.btn_member_list.setVisible(is_multi_member and self.workflow_is_linked)
            self.btn_disable_autorun.setVisible(is_multi_member and self.workflow_is_linked)
            self.btn_view.setVisible(is_multi_member and self.workflow_is_linked)
            any_is_agent = any(m.member_type == 'agent' for m in self.parent.members_in_view.values())
            # any_is_block = any(m.member_type == 'block' for m in self.parent.members_in_view.values())
            is_chat_workflow = self.parent.__class__.__name__ == 'ChatWorkflowSettings'
            param_list = self.parent.workflow_params.config.get('data', [])
            has_params = len(param_list) > 0
            self.btn_save_as.setVisible(is_multi_member or is_chat_workflow)
            self.btn_workflow_params.setVisible(is_multi_member or not any_is_agent or has_params)
            self.btn_workflow_config.setVisible(is_multi_member or not any_is_agent)

            self.btn_workflow_params.setChecked(has_params)
            self.toggle_workflow_params()

        def open_workspace(self):
            page_chat = self.parent.main.page_chat
            if page_chat.workspace_window is None:  # Check if the secondary window is not already open
                page_chat.workspace_window = WorkspaceWindow(page_chat)
                page_chat.workspace_window.setAttribute(
                    Qt.WA_DeleteOnClose)  # Ensure the secondary window is deleted when closed
                page_chat.workspace_window.destroyed.connect(
                    self.on_secondary_window_closed)  # Handle window close event
                page_chat.workspace_window.show()
            else:
                page_chat.workspace_window.raise_()
                page_chat.workspace_window.activateWindow()

        def on_secondary_window_closed(self):
            page_chat = self.parent.main.page_chat
            page_chat.workspace_window = None  # Reset the reference when the secondary window is closed

        def show_add_context_menu(self):
            menu = QMenu(self)

            add_agent = menu.addAction('Agent')
            add_user = menu.addAction('User')
            add_text = menu.addAction('Text')
            add_code = menu.addAction('Code')
            add_prompt = menu.addAction('Prompt')
            add_node = menu.addAction('Node')
            # add_tool = menu.addAction('Tool')
            add_agent.triggered.connect(partial(self.choose_member, "AGENT"))
            add_user.triggered.connect(partial(
                self.parent.add_insertable_entity,
                {'avatar': '', 'config': '{"_TYPE": "user"}', 'id': 0, 'name': 'You'}
            ))
            add_node.triggered.connect(partial(
                self.parent.add_insertable_entity,
                {'avatar': '', 'config': '{"_TYPE": "node"}', 'id': 0, 'name': 'Node'}
            ))
            # add_tool.triggered.connect(partial(self.choose_member, "TOOL"))

            add_text.triggered.connect(partial(self.choose_member, "TEXT"))
            add_code.triggered.connect(partial(self.choose_member, "CODE"))
            add_prompt.triggered.connect(partial(self.choose_member, "PROMPT"))

            menu.exec_(QCursor.pos())

        def choose_member(self, list_type):
            self.parent.set_edit_mode(True)
            list_dialog = TreeDialog(
                parent=self,
                title="Add Member",
                list_type=list_type,
                callback=self.parent.add_insertable_entity,
            )
            list_dialog.open()

        def show_save_context_menu(self):
            menu = QMenu(self)
            save_agent = menu.addAction('Save as Agent')
            save_agent.triggered.connect(partial(self.save_as, 'AGENT'))
            save_block = menu.addAction('Save as Block')
            save_block.triggered.connect(partial(self.save_as, 'BLOCK'))
            save_tool = menu.addAction('Save as Tool')
            save_tool.triggered.connect(partial(self.save_as, 'TOOL'))
            menu.exec_(QCursor.pos())

        def save_as(self, save_type):
            new_name, ok = QInputDialog.getText(self, f"New {save_type.capitalize()}", f"Enter the name for the new {save_type.lower()}:")
            if not ok:
                return

            workflow_config = json.dumps(self.parent.get_config())
            try:
                if save_type == 'AGENT':
                    sql.execute("""
                        INSERT INTO entities (name, kind, config)
                        VALUES (?, ?, ?)
                    """, (new_name, 'AGENT', workflow_config,))

                elif save_type == 'BLOCK':
                    sql.execute("""
                        INSERT INTO blocks (name, config)
                        VALUES (?, ?)
                    """, (new_name, workflow_config,))
                elif save_type == 'TOOL':
                    sql.execute("""
                        INSERT INTO tools (uuid, name, config)
                        VALUES (?, ?, ?)
                    """, (str(uuid.uuid4()), new_name, workflow_config,))

                display_messagebox(
                    icon=QMessageBox.Information,
                    title='Success',
                    text='Entity saved',
                )
            except sqlite3.IntegrityError as e:
                display_messagebox(
                    icon=QMessageBox.Warning,
                    title='Error',
                    text='Name already exists',
                )

        def clear_chat(self):
            retval = display_messagebox(
                icon=QMessageBox.Warning,
                text="Are you sure you want to permanently clear the chat messages?\nThis should only be used when testing a workflow.\nTo keep your data start a new chat.",
                title="Clear Chat",
                buttons=QMessageBox.Ok | QMessageBox.Cancel,
            )
            if retval != QMessageBox.Ok:
                return

            workflow = self.parent.linked_workflow()
            if not workflow:
                return

            sql.execute("""
                WITH RECURSIVE delete_contexts(id) AS (
                    SELECT id FROM contexts WHERE id = ?
                    UNION ALL
                    SELECT contexts.id FROM contexts
                    JOIN delete_contexts ON contexts.parent_id = delete_contexts.id
                )
                DELETE FROM contexts WHERE id IN delete_contexts AND id != ?;
            """, (workflow.context_id, workflow.context_id,))
            sql.execute("""
                WITH RECURSIVE delete_contexts(id) AS (
                    SELECT id FROM contexts WHERE id = ?
                    UNION ALL
                    SELECT contexts.id FROM contexts
                    JOIN delete_contexts ON contexts.parent_id = delete_contexts.id
                )
                DELETE FROM contexts_messages WHERE context_id IN delete_contexts;
            """, (workflow.context_id,))
            sql.execute("""
            DELETE FROM contexts_messages WHERE context_id = ?""",
                        (workflow.context_id,))

            if hasattr(self.parent.parent, 'main'):
                self.parent.parent.main.page_chat.load()

        def toggle_member_list(self):  # todo refactor
            # # if self.btn_workflow_config.isChecked():
            # #     self.btn_workflow_config.setChecked(False)
            # #     self.parent.workflow_config.setVisible(False)
            # self.untoggle_all(except_obj=self.btn_member_list)
            is_checked = self.btn_member_list.isChecked()
            self.parent.member_list.setVisible(is_checked)

        def toggle_workflow_params(self):
            # if self.btn_workflow_params.isChecked():
            #     self.btn_workflow_params.setChecked(False)
            #     self.parent.workflow_params.setVisible(False)
            self.untoggle_all(except_obj=self.btn_workflow_params)
            is_checked = self.btn_workflow_params.isChecked()
            self.parent.workflow_params.setVisible(is_checked)

        def toggle_workflow_config(self):
            # if self.btn_member_list.isChecked():
            #     self.btn_member_list.setChecked(False)
            #     self.parent.member_list.setVisible(False)
            self.untoggle_all(except_obj=self.btn_workflow_config)
            is_checked = self.btn_workflow_config.isChecked()
            self.parent.workflow_config.setVisible(is_checked)

        def untoggle_all(self, except_obj=None):
            # if self.btn_member_list.isChecked() and except_obj != self.btn_member_list:
            #     self.btn_member_list.setChecked(False)
            #     self.parent.member_list.setVisible(False)
            if self.btn_workflow_params.isChecked() and except_obj != self.btn_workflow_params:
                self.btn_workflow_params.setChecked(False)
                self.parent.workflow_params.setVisible(False)
            if self.btn_workflow_config.isChecked() and except_obj != self.btn_workflow_config:
                self.btn_workflow_config.setChecked(False)
                self.parent.workflow_config.setVisible(False)

        def btn_view_clicked(self):
            menu = QMenu(self)
            show_hidden = menu.addAction('Show hidden bubbles')
            show_nested = menu.addAction('Show nested bubbles')
            show_hidden.setCheckable(True)
            show_nested.setCheckable(True)
            show_hidden.setChecked(self.show_hidden_bubbles)
            show_nested.setChecked(self.show_nested_bubbles)
            show_hidden.triggered.connect(partial(self.toggle_attribute, 'show_hidden_bubbles'))
            show_nested.triggered.connect(partial(self.toggle_attribute, 'show_nested_bubbles'))

            self.btn_view.setChecked(self.show_hidden_bubbles or self.show_nested_bubbles)

            # top right corner is at cursor position
            menu.exec_(QCursor.pos() - QPoint(menu.sizeHint().width(), 0))

        def toggle_attribute(self, attr):
            setattr(self, attr, not getattr(self, attr))
            self.parent.save_config()
            self.btn_view.setChecked(self.show_hidden_bubbles or self.show_nested_bubbles)
            # if not self.parent.compact_mode:
            #     self.parent.parent.load()

    class MemberList(QWidget):
        """This widget displays a list of members in the chat."""
        def __init__(self, parent):
            super().__init__(parent)
            self.parent = parent
            self.block_flag = False  # todo clean

            self.layout = CVBoxLayout(self)
            self.layout.setContentsMargins(0, 5, 0, 0)

            self.tree_members = BaseTreeWidget(self, row_height=15)
            self.schema = [
                {
                    'text': 'Members',
                    'type': str,
                    'width': 150,
                    'image_key': 'avatar',
                },
                {
                    'key': 'id',
                    'text': '',
                    'type': int,
                    'visible': False,
                },
                {
                    'key': 'avatar',
                    'text': '',
                    'type': str,
                    'visible': False,
                },
            ]
            self.tree_members.itemSelectionChanged.connect(self.on_selection_changed)
            self.tree_members.build_columns_from_schema(self.schema)
            self.tree_members.setFixedWidth(150)
            self.layout.addWidget(self.tree_members)
            self.layout.addStretch(1)

        def load(self):
            selected_ids = self.tree_members.get_selected_item_ids()
            data = [
                [
                    get_member_name_from_config(m.config),
                    m.member_id,
                    get_avatar_paths_from_config(m.config, merge_multiple=True),
                ]
                for m in self.parent.linked_workflow().members.values()
            ]
            self.tree_members.load(
                data=data,
                folders_data=[],
                schema=self.schema,
                readonly=True,
                silent_select_id=selected_ids,
            )
            # set height to fit all items & header
            height = self.tree_members.sizeHintForRow(0) * (len(data) + 1)
            self.tree_members.setFixedHeight(height)

        def on_selection_changed(self):
            # push selection to view
            all_selected_ids = self.tree_members.get_selected_item_ids()
            self.block_flag = True
            self.parent.select_ids(all_selected_ids, send_signal=False)
            self.block_flag = False

        def refresh_selected(self):
            # get selection from view
            if self.block_flag:
                return
            selected_objects = self.parent.scene.selectedItems()
            selected_members = [x for x in selected_objects if isinstance(x, DraggableMember)]
            selected_member_ids = [m.id for m in selected_members]
            with block_signals(self.tree_members):
                self.tree_members.select_items_by_id(selected_member_ids)

        #     self.select_member_list_ids(selected_member_ids)
        #
        # def select_member_list_ids(self, ids):
        #     self.tree_members.select_items(ids)

    class WorkflowParams(ConfigJsonTree):
        def __init__(self, parent):
            super().__init__(parent=parent,
                             add_item_prompt=('NA', 'NA'),
                             del_item_prompt=('NA', 'NA'),)
                             # config_type=list)
            self.parent = parent
            # self.conf_namespace = 'parameters'
            self.hide()
            self.schema = [
                {
                    'text': 'Name',
                    'type': str,
                    'width': 120,
                    'default': '< Enter a parameter name >',
                },
                {
                    'text': 'Description',
                    'type': str,
                    'stretch': True,
                    'default': '< Enter a description >',
                },
                {
                    'text': 'Type',
                    'type': ('String', 'Int', 'Float', 'Bool',),
                    'width': 100,
                    'on_edit_reload': True,
                    'default': 'String',
                },
                {
                    'text': 'Req',
                    'type': bool,
                    'default': True,
                },
            ]

    class WorkflowConfig(ConfigJoined):
        def __init__(self, parent):
            super().__init__(parent=parent, layout_type=QVBoxLayout)
            self.widgets = [
                self.WorkflowFields(self),
                # self.TabParameters(self),
            ]
            self.hide()

        class WorkflowFields(ConfigFields):
            def __init__(self, parent):
                super().__init__(parent=parent)
                self.parent = parent
                self.schema = [
                    {
                        'text': 'Filter role',
                        'type': 'RoleComboBox',
                        'width': 90,
                        'tooltip': 'Filter the output to a specific role, only used by nested workflows',
                        'default': 'All',
                    },
                ]


class CustomGraphicsView(QGraphicsView):
    coordinatesChanged = Signal(QPoint)

    def __init__(self, scene, parent):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.parent = parent

        self._is_panning = False
        self._mouse_press_pos = None
        self._mouse_press_scroll_x_val = None
        self._mouse_press_scroll_y_val = None

        self.temp_block_move_flag = False

        self.setMinimumHeight(200)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        from src.gui.style import TEXT_COLOR
        from src.utils.helpers import apply_alpha_to_hex
        self.setBackgroundBrush(QBrush(QColor(apply_alpha_to_hex(TEXT_COLOR, 0.05))))
        self.setFrameShape(QFrame.Shape.NoFrame)

        # self.hide()

    def contextMenuEvent(self, event):
        menu = QMenu(self)

        selected_items = self.parent.scene.selectedItems()
        if selected_items:
            # menu.addAction("Cut")
            menu.addAction("Copy")
        menu.addAction("Paste")

        if selected_items:
            menu.addAction("Delete")

        if len(selected_items) > 1:
            menu.addSeparator()
            menu.addAction("Group")
            # menu.addAction("Ungroup")

        # Show the menu and get the chosen action
        chosen_action = menu.exec(event.globalPos())

        if chosen_action:
            if chosen_action.text() == "Copy":
                self.copy_selected_items()
            elif chosen_action.text() == "Delete":
                self.delete_selected_items()
            elif chosen_action.text() == "Paste":
                self.paste_items()

        # if chosen_action == delete_action:
        #     for item in selected_items:
        #         self.scene.removeItem(item)
        # elif chosen_action:
        #     # Handle other actions
        #     print(f"Action: {chosen_action.text()} for {len(selected_items)} items")

    def copy_selected_items(self):
        member_configs = []
        for selected_item in self.scene().selectedItems():
            if isinstance(selected_item, DraggableMember):
                member_configs.append(selected_item.member_config)
        # add to clipboard
        clipboard = QApplication.clipboard()
        clipboard.setText(json.dumps(member_configs))

    def paste_items(self):
        clipboard = QApplication.clipboard()
        try:
            member_configs = json.loads(clipboard.text())
            if not isinstance(member_configs, list):
                return
            if not all(isinstance(x, dict) for x in member_configs):
                return
            for member_config in member_configs:
                self.parent.add_entity(member_config)
        except Exception as e:
            return

    def cancel_new_line(self):
        # Remove the temporary line from the scene and delete it
        self.scene().removeItem(self.parent.new_line)
        self.parent.new_line = None
        self.update()

    def cancel_new_entity(self):
        # Remove the new entity from the scene and delete it
        self.scene().removeItem(self.parent.new_agent)
        self.parent.new_agent = None
        self.update()

        can_simplify_view = self.parent.can_simplify_view()  # todo merge duplicate code
        if can_simplify_view:
            self.parent.toggle_view(False)  # .view.hide()  # !68! # 31
            # Select the member so that it's config is shown, then hide the workflow panel until more members are added
            other_member_ids = [k for k, m in self.parent.members_in_view.items() if not m.member_config.get('_TYPE', 'agent') == 'user']  # .member_type != 'user']
            if other_member_ids:
                self.parent.select_ids([other_member_ids[0]])

    def delete_selected_items(self):
        del_member_ids = set()
        del_inputs = set()
        all_del_objects = []
        all_del_objects_old_brushes = []
        all_del_objects_old_pens = []

        for selected_item in self.parent.scene.selectedItems():
            all_del_objects.append(selected_item)

            if isinstance(selected_item, DraggableMember):
                del_member_ids.add(selected_item.id)

                # Loop through all lines to find the ones connected to the selected agent
                for key, line in self.parent.lines.items():
                    member_id, input_member_id = key
                    if member_id == selected_item.id or input_member_id == selected_item.id:
                        del_inputs.add((member_id, input_member_id))
                        all_del_objects.append(line)

            elif isinstance(selected_item, ConnectionLine):
                del_inputs.add((selected_item.member_id, selected_item.input_member_id))

        del_count = len(del_member_ids) + len(del_inputs)
        if del_count == 0:
            return

        # fill all objects with a red tint at 30% opacity, overlaying the current item image
        for item in all_del_objects:
            old_brush = item.brush()
            all_del_objects_old_brushes.append(old_brush)
            # modify old brush and add a 30% opacity red fill
            old_pixmap = old_brush.texture()
            new_pixmap = old_pixmap.copy()
            painter = QPainter(new_pixmap)
            painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)

            painter.fillRect(new_pixmap.rect(),
                             QColor(255, 0, 0, 126))
            painter.end()
            new_brush = QBrush(new_pixmap)
            item.setBrush(new_brush)

            old_pen = item.pen()
            all_del_objects_old_pens.append(old_pen)
            new_pen = QPen(QColor(255, 0, 0, 255),
                           old_pen.width())
            item.setPen(new_pen)

        self.parent.scene.update()

        # ask for confirmation
        retval = display_messagebox(
            icon=QMessageBox.Warning,
            text="Are you sure you want to delete the selected items?",
            title="Delete Items",
            buttons=QMessageBox.Ok | QMessageBox.Cancel,
        )
        if retval != QMessageBox.Ok:
            for item in all_del_objects:
                item.setBrush(all_del_objects_old_brushes.pop(0))
                item.setPen(all_del_objects_old_pens.pop(0))
            return

        for obj in all_del_objects:
            self.parent.scene.removeItem(obj)

        for member_id in del_member_ids:
            self.parent.members_in_view.pop(member_id)
        for line_key in del_inputs:
            self.parent.lines.pop(line_key)

        self.parent.save_config()
        if hasattr(self.parent.parent, 'top_bar'):
            self.parent.parent.load()
        # if not self.parent.compact_mode:
        #     self.parent.parent.reload_selected_item()

    def mouse_is_over_member(self):
        mouse_scene_position = self.mapToScene(self.mapFromGlobal(QCursor.pos()))
        for member_id, member in self.parent.members_in_view.items():
            # We need to map the scene position to the member's local coordinates
            member_local_pos = member.mapFromScene(mouse_scene_position)
            if member.contains(member_local_pos):
                return True
        return False

    def mouseReleaseEvent(self, event):
        self._is_panning = False
        self._mouse_press_pos = None
        self._mouse_press_scroll_x_val = None
        self._mouse_press_scroll_y_val = None
        # self.temp_block_move_flag = False
        super().mouseReleaseEvent(event)
        main = find_main_widget(self)
        main.mouseReleaseEvent(event)

    def mousePressEvent(self, event):
        # todo
        self.temp_block_move_flag = False

        if self.parent.new_agent:
            self.parent.add_entity()
            return

        # Check if the mouse is over a member and want to drag it, and not activate panning
        if self.mouse_is_over_member():
            self._is_panning = False
            self._mouse_press_pos = None
            self._mouse_press_scroll_x_val = None
            self._mouse_press_scroll_y_val = None
        else:
            # Otherwise, continue with the original behavior
            if event.button() == Qt.LeftButton:
                self._is_panning = True
                self._mouse_press_pos = event.pos()
                self._mouse_press_scroll_x_val = self.horizontalScrollBar().value()
                self._mouse_press_scroll_y_val = self.verticalScrollBar().value()

        mouse_scene_position = self.mapToScene(event.pos())
        for member_id, member in self.parent.members_in_view.items():
            if isinstance(member, DraggableMember):
                member_width = member.rect().width()
                input_rad = 20  # int(member_width / 2)
                if self.parent.new_line:
                    input_point_pos = member.input_point.scenePos()
                    # if within 20px
                    if (mouse_scene_position - input_point_pos).manhattanLength() <= input_rad:
                        self.parent.add_input(member_id)
                        return
                else:
                    output_point_pos = member.output_point.scenePos()
                    output_point_pos.setX(output_point_pos.x() + 8)
                    # if within 20px
                    if (mouse_scene_position - output_point_pos).manhattanLength() <= input_rad:
                        self.parent.new_line = ConnectionLine(self.parent, member)
                        self.parent.scene.addItem(self.parent.new_line)
                        return

        pass
        # If click anywhere else, cancel the new line
        if self.parent.new_line:
            self.scene().removeItem(self.parent.new_line)
            self.parent.new_line = None

        super().mousePressEvent(event)  # !! #

    def mouseMoveEvent(self, event):
        update = False
        mouse_point = self.mapToScene(event.pos())
        if self.parent.new_line:
            self.parent.new_line.updateEndPoint(mouse_point)
            update = True
        if self.parent.new_agent:
            self.parent.new_agent.setCentredPos(mouse_point)
            update = True

        if update:
            if self.scene():
                self.scene().update()
            self.update()

        if self._is_panning:
            delta = event.pos() - self._mouse_press_pos
            self.horizontalScrollBar().setValue(self._mouse_press_scroll_x_val - delta.x())
            self.verticalScrollBar().setValue(self._mouse_press_scroll_y_val - delta.y())

        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.parent.new_line:
                self.cancel_new_line()
            if self.parent.new_agent:
                self.cancel_new_entity()

        elif event.key() == Qt.Key_Delete:
            if self.parent.new_line:
                self.cancel_new_line()
                return
            if self.parent.new_agent:
                self.cancel_new_entity()
                return

            self.delete_selected_items()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        # # set view to top left
        tl = self.mapToScene(self.viewport().rect().topLeft())
        if tl.x() < 0 or tl.y() < 0:
            self.centerOn(tl)


class InsertableMember(QGraphicsEllipseItem):
    def __init__(self, parent, config, pos):
        self.member_type = config.get('_TYPE', 'agent')
        self.member_config = config
        diameter = 50 if self.member_type != 'node' else 20
        super().__init__(0, 0, diameter, diameter)
        # super().__init__(0, 0, 50, 50)
        # self.member_type = config.get('_TYPE', 'agent')
        # self.member_config = config
        from src.gui.style import TEXT_COLOR

        self.parent = parent
        # self.id = agent_id
        member_type = config.get('_TYPE', 'agent')
        self.config = config

        pen = QPen(QColor(TEXT_COLOR), 1)

        if member_type in ['workflow', 'tool', 'block']:
            pen = None
        self.setPen(pen if pen else Qt.NoPen)
        self.refresh_avatar()

        self.setCentredPos(pos)

    def refresh_avatar(self):
        from src.gui.style import TEXT_COLOR
        if self.member_type == 'node':
            self.setBrush(QBrush(QColor(TEXT_COLOR)))
            return

        hide_bubbles = self.config.get('group.hide_bubbles', False)
        opacity = 0.2 if hide_bubbles else 1

        avatar_paths = get_avatar_paths_from_config(self.config)

        diameter = 50
        pixmap = path_to_pixmap(avatar_paths, opacity=opacity, diameter=diameter)  # , def_avatar=def_avatar)

        if pixmap:
            self.setBrush(QBrush(pixmap.scaled(diameter, diameter)))

    def setCentredPos(self, pos):
        self.setPos(pos.x() - self.rect().width() / 2, pos.y() - self.rect().height() / 2)


class DraggableMember(QGraphicsEllipseItem):
    def __init__(self, parent, member_id, loc_x, loc_y, member_config):
        self.member_type = member_config.get('_TYPE', 'agent')
        self.member_config = member_config
        diameter = 50 if self.member_type != 'node' else 20
        super().__init__(0, 0, diameter, diameter)
        # super().__init__(0, 0, 50, 50)
        # self.member_type = member_config.get('_TYPE', 'agent')
        # self.member_config = member_config
        from src.gui.style import TEXT_COLOR

        self.parent = parent
        self.id = member_id

        # TEMP
        block_type = member_config.get('block_type', None)
        if block_type:
            if block_type == 'Prompt':
                pass

        pen = QPen(QColor(TEXT_COLOR), 1)

        if self.member_type in ['workflow', 'tool', 'block']:
            pen = None

        self.setPen(pen if pen else Qt.NoPen)

        self.setPos(loc_x, loc_y)

        self.refresh_avatar()

        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemIsSelectable)

        self.input_point = ConnectionPoint(self, True)
        self.output_point = ConnectionPoint(self, False)
        # self.input_point.setPos(0, self.rect().height() / 2)
        # self.output_point.setPos(self.rect().width() - 4, self.rect().height() / 2)
        # take into account the diameter of the points
        self.input_point.setPos(0, self.rect().height() / 2 - 2)
        self.output_point.setPos(self.rect().width() - 4, self.rect().height() / 2 - 2)

        self.setAcceptHoverEvents(True)

        # Create the highlight background item
        self.highlight_background = self.HighlightBackground(self)
        self.highlight_background.setPos(self.rect().width()/2, self.rect().height()/2)
        self.highlight_background.hide()  # Initially hidden

        # self.highlight_states = {
        #     'responding': '#0bde2b',
        #     'waiting': '#f7f7f7',
        # }

    def refresh_avatar(self):
        from src.gui.style import TEXT_COLOR
        if self.member_type == 'node':
            self.setBrush(QBrush(QColor(TEXT_COLOR)))
            return

        hide_bubbles = self.member_config.get('group.hide_bubbles', False)
        opacity = 0.2 if hide_bubbles else 1
        avatar_paths = get_avatar_paths_from_config(self.member_config)

        diameter = 50
        pixmap = path_to_pixmap(avatar_paths, opacity=opacity, diameter=diameter)  # , def_avatar=def_avatar)

        # if pixmap is not null
        if pixmap:
            self.setBrush(QBrush(pixmap.scaled(diameter, diameter)))
        pass

    def toggle_highlight(self, enable, color=None):
        """Toggles the visual highlight on or off."""
        if enable:
            self.highlight_background.use_color = color
            self.highlight_background.show()
        else:
            self.highlight_background.hide()

    def mouseMoveEvent(self, event):
        if self.output_point.contains(event.pos() - self.output_point.pos()):
            return

        if self.parent.new_line:
            return

        if self.parent.view.temp_block_move_flag:
            return

        # # if mouse not inside scene, return
        cursor = event.scenePos()
        # if not self.parent.view.rect().contains(cursor.toPoint()):
        #     return
        # # if mouse not positive, return
        if cursor.x() < 0 or cursor.y() < 0:
            return

        super().mouseMoveEvent(event)
        for line in self.parent.lines.values():
            line.updatePosition()

        if self.member_type != 'node':
            self.parent.load_async_groups()
            self.parent.refresh_member_highlights()

    def mouseReleaseEvent(self, event):  # this is faster
        super().mouseReleaseEvent(event)
        # with block_signals(self.scene()):
        self.save_pos()

    def save_pos(self):
        new_loc_x = max(0, int(self.x()))
        new_loc_y = max(0, int(self.y()))
        members = self.parent.config.get('members', [])  # todo dirty
        member = next((m for m in members if m['id'] == self.id), None)
        if member:
            if new_loc_x == member['loc_x'] and new_loc_y == member['loc_y']:
                return
        self.parent.update_member([
            (self.id, 'loc_x', new_loc_x),
            (self.id, 'loc_y', new_loc_y)
        ])
        self.parent.save_config()

    def hoverMoveEvent(self, event):
        # Check if the mouse is within 20 pixels of the output point
        if self.output_point.contains(event.pos() - self.output_point.pos()):
            self.output_point.setHighlighted(True)
        else:
            self.output_point.setHighlighted(False)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.output_point.setHighlighted(False)
        super().hoverLeaveEvent(event)

    class HighlightBackground(QGraphicsItem):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.inner_diameter = parent.rect().width()  # Diameter of the hole, same as the DraggableMember's ellipse
            self.outer_diameter = int(self.inner_diameter * 1.6)  # Diameter including the gradient
            self.use_color = None  # Uses text color when none

        def boundingRect(self):
            return QRectF(-self.outer_diameter / 2, -self.outer_diameter / 2, self.outer_diameter, self.outer_diameter)

        def paint(self, painter, option, widget=None):
            from src.gui.style import TEXT_COLOR
            gradient = QRadialGradient(QPointF(0, 0), self.outer_diameter / 2)
            # text_color_ = QColor(TEXT_COLOR)
            color = self.use_color or QColor(TEXT_COLOR)
            color.setAlpha(155)
            gradient.setColorAt(0, color)  # Inner color of gradient
            gradient.setColorAt(1, QColor(255, 255, 0, 0))  # Outer color of gradient

            # Create a path for the outer ellipse (gradient)
            outer_path = QPainterPath()
            outer_path.addEllipse(-self.outer_diameter / 2, -self.outer_diameter / 2, self.outer_diameter,
                                  self.outer_diameter)

            # Create a path for the inner hole
            inner_path = QPainterPath()
            inner_path.addEllipse(-self.inner_diameter / 2, -self.inner_diameter / 2, self.inner_diameter,
                                  self.inner_diameter)

            # Subtract the inner hole from the outer path
            final_path = QPainterPath(outer_path)
            final_path = final_path.subtracted(inner_path)

            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)  # No border
            painter.drawPath(final_path)


class ConnectionLine(QGraphicsPathItem):
    def __init__(self, parent, input_member, member=None, config=None):  # input_type=0):  # key, start_point, end_point=None, input_type=0):
        super().__init__()
        from src.gui.style import TEXT_COLOR
        self.parent = parent
        self.input_member_id = input_member.id
        self.member_id = member.id if member else None
        self.start_point = input_member.output_point
        self.end_point = member.input_point if member else None
        self.selection_path = None
        self.looper_midpoint = None

        self.config = config if config else {}
        # self.input_type = self.config.get('input_type', 'Message')

        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.color = QColor(TEXT_COLOR)

        self.updatePath()

        self.setPen(QPen(self.color, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self.setZValue(-1)

    def paint(self, painter, option, widget):
        line_width = 4 if self.isSelected() else 2
        current_pen = self.pen()
        current_pen.setWidth(line_width)
        input_type = self.config.get('input_type', 'Message')
        # set to a dashed line if input type is 1
        if input_type == 'Message':
            current_pen.setStyle(Qt.SolidLine)
        elif input_type == 'Flow':
            current_pen.setStyle(Qt.DashLine)
        painter.setPen(current_pen)
        painter.drawPath(self.path())

        # # # draw a left arrow from the midpoint of the line with each arrow line being 10 pixels long
        # # painter.drawLine(self.looper_midpoint, self.looper_midpoint + QPointF(10, 5))
        # # painter.drawLine(self.looper_midpoint, self.looper_midpoint + QPointF(10, -5))
        # # make it an opaque triangle
        if self.looper_midpoint:
            painter.setBrush(QBrush(self.color))
            painter.drawPolygon(QPolygonF([self.looper_midpoint, self.looper_midpoint + QPointF(10, 5), self.looper_midpoint + QPointF(10, -5)]))


        # # draw text at the midpoint of the line,
        # if self.looper_midpoint:
        #     painter.drawText(self.looper_midpoint + QPointF(-10, -5), '5')

    def updateEndPoint(self, end_point):
        # self.end_point = end_point
        # self.updatePath()

        # find closest start point and if it's within 20px, snap to it
        # if not, set the end point to the cursor position

        # find closest start point
        closest_member_id = None
        closest_start_point = None
        closest_distance = 1000
        for member_id, member in self.parent.members_in_view.items():
            if member_id == self.input_member_id:
                continue
            start_point = member.input_point.scenePos()
            distance = (start_point - end_point).manhattanLength()
            if distance < closest_distance:
                closest_distance = distance
                closest_start_point = start_point
                closest_member_id = member_id

        if closest_distance < 20:
            self.end_point = closest_start_point
            cr_check = self.parent.check_for_circular_references(closest_member_id, [self.input_member_id])
            self.config['looper'] = True if cr_check else False
        else:
            self.end_point = end_point
            self.config['looper'] = False
        self.updatePath()

    def updatePosition(self):
        self.updatePath()
        self.scene().update(self.scene().sceneRect())

    def updatePath(self):
        if self.end_point is None:
            return
        start_point = self.start_point.scenePos() if isinstance(self.start_point, ConnectionPoint) else self.start_point
        end_point = self.end_point.scenePos() if isinstance(self.end_point, ConnectionPoint) else self.end_point

        # start point += (2, 2)
        start_point = start_point + QPointF(2, 2)
        end_point = end_point + QPointF(2, 2)

        is_looper = self.config.get('looper', False)

        if start_point.y() == end_point.y():
            pass
        tmp_end_pnt = end_point
        if is_looper:
            line_is_under = start_point.y() >= end_point.y()
            if (line_is_under and start_point.y() > end_point.y()) or (start_point.y() < end_point.y() and not line_is_under):
                extender_side = 'left'
            else:
                extender_side = 'right'
            y_diff = abs(start_point.y() - end_point.y())
            if not line_is_under:
                y_diff = -y_diff

            path = QPainterPath(start_point)

            x_rad = 25
            y_rad = 25 if line_is_under else -25

            # Draw half of the right side of the loop
            cp1 = QPointF(start_point.x() + x_rad, start_point.y())
            cp2 = QPointF(start_point.x() + x_rad, start_point.y() + y_rad)
            path.cubicTo(cp1, cp2, QPointF(start_point.x() + x_rad, start_point.y() + y_rad))

            if extender_side == 'right':
                # Draw a vertical line
                path.lineTo(QPointF(start_point.x() + x_rad, start_point.y() + y_rad + y_diff))

            # Draw the other half of the right hand side loop
            var = y_diff if extender_side == 'right' else 0
            cp3 = QPointF(start_point.x() + x_rad, start_point.y() + y_rad + var + y_rad)
            cp4 = QPointF(start_point.x(), start_point.y() + y_rad + var + y_rad)
            path.cubicTo(cp3, cp4, QPointF(start_point.x(), start_point.y() + y_rad + var + y_rad))

            # Draw the horizontal line
            x_diff = start_point.x() - end_point.x()
            if x_diff < 50:
                x_diff = 50
            path.lineTo(QPointF(start_point.x() - x_diff, start_point.y() + y_rad + var + y_rad))
            self.looper_midpoint = QPointF(start_point.x() - (x_diff / 2), start_point.y() + y_rad + var + y_rad)

            # Draw half of the left side of the loop
            line_to = QPointF(start_point.x() - x_diff - x_rad, start_point.y() + y_rad + var)
            cp5 = QPointF(start_point.x() - x_diff - x_rad, start_point.y() + y_rad + var + y_rad)
            cp6 = line_to
            path.cubicTo(cp5, cp6, line_to)

            if extender_side == 'left':
                # Draw the vertical line up y_diff pixels
                line_to = QPointF(start_point.x() - x_diff - x_rad, start_point.y() + y_rad - y_diff)
                path.lineTo(line_to)
            else:
                # Draw the vertical line down y_diff pixels
                line_to = QPointF(start_point.x() - x_diff - x_rad, start_point.y() + y_rad + y_diff)
                path.lineTo(line_to)

            # Draw the other half of the left hand side loop
            # cp7 = QPointF(start_point.x() - x_diff - 25, start_point.y() + 25 - y_diff - 25)
            # cp8 = QPointF(start_point.x(), start_point.y() + 25 - y_diff - 25)
            diag_pt_top_right = QPointF(line_to.x() + x_rad, line_to.y() - y_rad)
            # diag_pt_top_right = line_to + QPointF(25, 25 * (-1 if line_is_under else 1))
            cp7 = QPointF(diag_pt_top_right.x() - x_rad, diag_pt_top_right.y() + y_rad)
            cp8 = QPointF(diag_pt_top_right.x() - x_rad, diag_pt_top_right.y())
            path.cubicTo(cp7, cp8, diag_pt_top_right)

            # Draw line to the end point
            path.lineTo(end_point)
        else:
            x_distance = (end_point - start_point).x()
            y_distance = abs((end_point - start_point).y())

            # Set control points offsets to be a fraction of the horizontal distance
            fraction = 0.61  # Adjust the fraction as needed (e.g., 0.2 for 20%)
            offset = x_distance * fraction
            if offset < 0:
                offset *= 3
                offset = min(offset, -40)
            else:
                offset = max(offset, 40)
                offset = min(offset, y_distance)
            offset = abs(offset)  # max(abs(offset), 10)

            path = QPainterPath(start_point)
            ctrl_point1 = start_point + QPointF(offset, 0)
            ctrl_point2 = end_point - QPointF(offset, 0)
            path.cubicTo(ctrl_point1, ctrl_point2, end_point)
            self.looper_midpoint = None

        self.setPath(path)
        self.updateSelectionPath()

    def updateSelectionPath(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(20)
        self.selection_path = stroker.createStroke(self.path())

    def shape(self):
        if self.selection_path is None:
            return super().shape()
        return self.selection_path


class ConnectionPoint(QGraphicsEllipseItem):
    def __init__(self, parent, is_input):
        radius = 2
        super().__init__(0, 0, 2 * radius, 2 * radius, parent)
        self.is_input = is_input
        self.setBrush(QBrush(Qt.darkGray if is_input else Qt.darkRed))
        self.connections = []

    def setHighlighted(self, highlighted):
        if highlighted:
            self.setBrush(QBrush(Qt.red))
        else:
            self.setBrush(QBrush(Qt.black))

    def contains(self, point):
        distance = (point - self.rect().center()).manhattanLength()
        return distance <= 12


class RoundedRectWidget(QGraphicsWidget):
    def __init__(self, parent, points, member_ids, rounding_radius=25):
        super().__init__()
        self.parent = parent
        self.member_ids = member_ids

        self.rounding_radius = rounding_radius
        self.setZValue(-2)

        # points is a list of QPointF points, all must be within the bounds
        lowest_x = min([point.x() for point in points])
        lowest_y = min([point.y() for point in points])
        btm_left = QPointF(lowest_x, lowest_y)

        highest_x = max([point.x() for point in points])
        highest_y = max([point.y() for point in points])
        top_right = QPointF(highest_x, highest_y)

        # Calculate width and height from l_bound and u_bound
        width = abs(btm_left.x() - top_right.x()) + 50
        height = abs(btm_left.y() - top_right.y()) + 50

        # Set size policy and preferred size
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setPreferredSize(width, height)

        # Set the position based on the l_bound
        self.setPos(btm_left)

    def boundingRect(self):
        return QRectF(0, 0, self.preferredWidth(), self.preferredHeight())

    def paint(self, painter, option, widget):
        from src.gui.style import TEXT_COLOR
        rect = self.boundingRect()
        painter.setRenderHint(QPainter.Antialiasing)

        # Set brush with 20% opacity color
        color = QColor(TEXT_COLOR)
        color.setAlpha(50)
        painter.setBrush(QBrush(color))

        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(rect, self.rounding_radius, self.rounding_radius)


class DynamicMemberConfigWidget(ConfigWidget):
    def __init__(self, parent):
        super().__init__(parent=parent)
        from src.system.plugins import get_plugin_agent_settings, get_plugin_block_settings
        self.parent = parent
        self.layout = CVBoxLayout(self)
        self.stacked_layout = QStackedLayout()
        self.layout.addLayout(self.stacked_layout)
        # self.stacked_layout.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # self.setFixedHeight(200)

        self.empty_widget = self.EmptySettings(parent)  # parent=parent)
        self.agent_settings = get_plugin_agent_settings(None)(parent)
        self.user_settings = self.UserMemberSettings(parent)
        self.workflow_settings = None
        # self.tool_settings = self.ToolMemberSettings(parent)
        self.block_settings = get_plugin_block_settings(None)(parent)
        self.input_settings = self.InputSettings(parent)

        self.user_settings.build_schema()
        self.agent_settings.build_schema()
        self.block_settings.build_schema()
        self.input_settings.build_schema()

        self.stacked_layout.addWidget(self.empty_widget)
        self.stacked_layout.addWidget(self.agent_settings)
        self.stacked_layout.addWidget(self.user_settings)
        # self.stacked_layout.addWidget(self.tool_settings)
        self.stacked_layout.addWidget(self.input_settings)
        self.stacked_layout.addWidget(self.block_settings)

    def load(self, temp_only_config=False):
        pass

    def display_config_for_member(self, member):
        from src.system.plugins import get_plugin_agent_settings, get_plugin_block_settings

        # if member is None:
        #     self.stacked_layout.setCurrentWidget(self.empty_widget)
        #     return

        member_type = member.member_type
        member_config = member.member_config

        type_widgets = {
            'agent': 'agent_settings',
            'user': 'user_settings',
            # 'tool': 'tool_settings',
            'block': 'block_settings',
            'workflow': 'workflow_settings',
            'node': 'empty_widget',
        }
        type_pluggable_classes = {
            'agent': get_plugin_agent_settings,
            'block': get_plugin_block_settings,
        }
        widget_name = type_widgets[member_type]
        # widget = getattr(self, widget_name)

        if member_type == "workflow":
            # added_tmp = False
            if self.workflow_settings is None:
                self.workflow_settings = self.WorkflowMemberSettings(self.parent)
                # added_tmp = True
                self.stacked_layout.addWidget(self.workflow_settings)
            self.workflow_settings.member_id = member.id
            self.workflow_settings.load_config(member_config)
            self.workflow_settings.load()

            # if added_tmp:
            #     self.stacked_layout.addWidget(self.workflow_settings)
            self.stacked_layout.setCurrentWidget(self.workflow_settings)
            self.workflow_settings.reposition_view()
            # QTimer.singleShot(100, lambda: self.reposition)  # not needed
            return

        elif member_type in type_pluggable_classes:
            class_func = type_pluggable_classes[member_type]
            if member_type == "agent":
                plugin_field = member_config.get('info.use_plugin', '')
            else:  # if member_type == "block":
                plugin_field = member_config.get('block_type', '')
            self.load_pluggable_member_config(widget_name, plugin_field, member, class_func)

        elif member_type == 'node':
            self.stacked_layout.setCurrentWidget(self.empty_widget)
            return
        else:
            getattr(self, widget_name).load_config(member.member_config)

        getattr(self, widget_name).load()
        self.stacked_layout.setCurrentWidget(getattr(self, widget_name))

    def load_pluggable_member_config(self, widget_name, plugin_field, member, class_func):
        if plugin_field == '':
            plugin_field = None

        old_widget = getattr(self, widget_name)
        current_plugin = getattr(old_widget, '_plugin_name', '')
        is_different = plugin_field != current_plugin

        if is_different:
            agent_settings_class = class_func(plugin_field)
            setattr(self, widget_name, agent_settings_class(self.parent))
            new_widget = getattr(self, widget_name)
            new_widget.build_schema()

            self.stacked_layout.addWidget(getattr(self, widget_name))
            self.stacked_layout.setCurrentWidget(getattr(self, widget_name))

            self.stacked_layout.removeWidget(old_widget)
            old_widget.deleteLater()

        getattr(self, widget_name).member_id = member.id
        getattr(self, widget_name).load_config(member.member_config)

    def display_config_for_input(self, line):  # member_id, input_member_id):
        member_id, input_member_id = line.member_id, line.input_member_id
        # self.current_input_key = (member_id, input_member_id)
        self.stacked_layout.setCurrentWidget(self.input_settings)
        self.input_settings.input_key = (member_id, input_member_id)
        self.input_settings.load_config(line.config)
        self.input_settings.load()

    class UserMemberSettings(UserSettings):
        def __init__(self, parent):
            super().__init__(parent)

        def update_config(self):
            self.save_config()

        def save_config(self):
            conf = self.get_config()
            self.parent.members_in_view[self.member_id].member_config = conf
            self.parent.save_config()

    class WorkflowMemberSettings(WorkflowSettings):
        def __init__(self, parent):
            super().__init__(parent, compact_mode=True)

        def update_config(self):
            self.save_config()

        def save_config(self):
            conf = self.get_config()
            self.parent.members_in_view[self.member_id].member_config = conf
            self.parent.save_config()

    class EmptySettings(ConfigFields):
        def __init__(self, parent):
            super().__init__(parent)
            self.schema = []

    class InputSettings(ConfigFields):
        def __init__(self, parent):
            super().__init__(parent)
            self.input_key = None
            self.schema = [
                {
                    'text': 'Input Type',
                    'type': ('Message', 'Flow'),
                    'default': 'Message',
                },
                {
                    'text': 'Looper',
                    'type': bool,
                    'default': False,
                },
            ]

        # def update_config(self):
        #     self.save_config()

        def save_config(self):
            conf = self.get_config()
            is_looper = conf.get('looper', False)
            reload = False
            if not is_looper:
                # check circular references #(member_id, [input_member_id])
                member_id = self.input_key[0]
                input_member_id = self.input_key[1]
                cr_check = self.parent.check_for_circular_references(member_id, [input_member_id])
                if cr_check:
                    display_messagebox(
                        icon=QMessageBox.Warning,
                        title='Warning',
                        text='Circular reference detected',
                        buttons=QMessageBox.Ok,
                    )
                    conf['looper'] = True  # todo bug
                    self.parent.lines[self.input_key].config = conf
                    self.looper.setChecked(True)
                    return

            self.parent.lines[self.input_key].config = conf
            self.parent.save_config()
            # repaint all lines
            graphics_item = self.parent.lines[self.input_key]
            graphics_item.updatePosition()
            if reload:  # temp
                self.load()


# Welcome to the tutorial! Here, we will walk you through a number of key concepts in Agent Pilot,
# starting with the basics and then moving on to more advanced features.

# -- BASICS --
# Agent Pilot provides a seamless experience, whether you want to chat with a single LLM, or a complex graph workflow.
#
# Let's start by adding our API keys in the settings.
# Click on the settings icon at the top of the sidebar, then click on the models tab.
# Here you'll see a list of all model providers that are currently available, with a field to enter an API key.
# Selecting a provider will list all the models available from it.
# Selecting one of these models will display all the parameters available for it.
# Agent pilot uses litellm for llm api calls, the model name here is sent with the API call,
# prefixed with `litellm_prefix` here, if supplied.
# Once you've added your API key, head back to the chat page by clicking the chat icon here.
# When on the chat page, it's icon will change to a + button, clicking this will create a new chat with the same config
# To open the config for the chat, click this area at the top.
# Here you can change the config for the workflow, go to the `Chat` tab and set its LLM model here.
# Try chatting with the assistant
# You can go back to previous messages and edit them, when we edit this message and resubmit, a branch is created
# You can cycle between these branches with these buttons
# To start a new chat, click this `+` button.
# The history of all your chats is saved in the Chats page here.
# Clicking on a chat will open it back up so that you can continue or refer back to it.
# You can quickly cycle between chats by using these navigation buttons
# Let's say you like this assistant configuration, you've set an LLM and a system prompt,
# but you want a different assistant for a different purpose, you can click here to save the assistant
# Type a name, and your agent will be saved in the entities page, go there by clicking here
# Selecting an agent will open its config, this is not tied to any chat, this config will be the
# default config for when the agent is used in a workflow. Unless this `+` button is clicked,
# in which case the config will be copied from this workflow.
# Start a new chat with an agent by double clicking on it.

# -- MULTI AGENT --
# Now that's the basics out of the way, lets go over how multi agent workflows work.
# In the chat page, open the workflow config.
# Click here to add a new member,
# Click on Agent and select one from the list, then drop it anywhere on the workflow
# This is a basic group chat with you and 2 other agents
# An important thing to note is that the order of response flows from left to right,
# so in this workflow, after you send a message, this agent will always respond first, followed by this agent.
# That is, unless an input is placed from this agent to this one, in this case,
# because the input of this one flows into this, this agent will respond first.
# Click on the member list button here to show the list of members, in the order they will respond.
# You should almost always have a user member at the beginning, this represents you.
# There can be multiple user members, so you can add your input at any point within a workflow

# Let's go over the context window of each agent, if the agent has no predefined inputs,
# then it can see all other agent messages, even the ones after it from previous turns
# But if an agent has inputs set like this one, then that agent will only see messages from the agents
# flowing into it.
# In this case this agent will output a response based on the direct output of this agent.
# The LLM will see this agents response in the form of a `user` LLM message.
# If an agent has multiple inputs, you can decide how to handle this in the agent config `group` tab
# By selecting an input, you can set which type of input to use,
# Message will send the output to the agent as user role,
# so it's like the agent is having a conversation with this agent
# A context input will not send as a message,
# but allows the agent output to be used in the context window of the next agents.
# You can do this using its output placeholder defined here,
# and use it in the system message of this agent using curly braces like this.
# Agents that are aligned vertically will run asynchronously, indicated by this highlighted bar.
#
# Lets use all of this in practice to create a simple mixture of agents workflow.
# These 2 agents can run asynchronously, and their only input is the user input.
# Set their models and output placeholders here.
# Add a new agent to use as the final agent, place it here and set its model.
# In the system message, we can use a prompt to combine the outputs of the previous agents,
# using their output placeholders, as defined here.
# Finally you can hide the bubbles for these agents by setting Hide bubbles to true here.
# Let's try chatting with this workflow.
# Those asynchronous agents should be working behind the scenes and the final agent should respond with a combined output.
# You can toggle the hidden bubbles by clicking this toggle icon here in the workflow settings.
# You can save the workflow as a single entity by clicking this save button, enter a name and click enter.
# Now any time you want to use this workflow just select it from the entities page.

# -- TOOLS --
# Now that you know how to setup multi agent workflows, let's go over tools.
# Tools are a way to add custom functionality to your agents, that the LLM can decide to call.
# Go to the settings page, and go to Tools
# Here you can see a list of tools, you can create a new tool by clicking the `+` button
# Give it a name, and a description, these are used by the LLM to help decide when to call it.
# In this method dropdown, you can select the method the LLM will use to call the tool.
# This can be a function call or Prompt based.
# To use function calling you have to use an LLM that supports it,
# For prompt based you can use any LLM, but it may not be as reliable.
# In this Code tab, you can write the code for the tool,
# depending on which type is selected in this dropdown, the code will be treated differently.
# The Native option wraps the code in a predefined function that's integrated into agent pilot.
# this function can be a generator, meaning ActionResponses can be 'yielded' aswell as 'returned',
# allowing the tool logic to continue sequentially from where it left off, after each user message.
# In the Parameters tab, you can define the parameters of the tool,
# These can be used from within the code using their names.
# Tools can be used by agents by adding them to their config, in the tools tab here.
# You can also use tools independently in a workflow by adding a tool member like this.
# Then you can use its output from another agents context using its name wrapped in curly braces.



# -- FILES --
# Files
# You can attach files to the chat, click here to upload a file, you can upload multiple files at once.

#  to get you comfortable with the interface.
#
# 1. We will then introduce the concept of branching, which allows you to explore different conversation paths.
# 2. Next, we will delve into chat settings. Here, you will learn how to customize your chat environment.
# 3. You will learn about the new button, which allows you to create new chat instances.
# 4. We will then add two more agents to the chat to demonstrate multi-agent interactions.
# 5. The loc_x order will be explained. This is crucial for understanding the flow of the conversation.
# 6. Next, we will introduce context windows, which give you a snapshot of the conversation at any given point.
# 7. We will then add an input in the opposite direction to demonstrate bidirectional communication.
# 8. We will explain the significance of order and context in the chat environment.
# 9. You will learn how to manage multiple inputs and outputs in the conversation.
# 10. The concept of output placeholders will be introduced.
# 11. You will learn how to save a conversation as an entity for future reference.
# 12. We will show you the agent list where all the agents in the conversation are listed.
# 13. We will open a workflow entity to demonstrate how it can be manipulated.
# 14. You will learn how to incorporate a workflow entity into your workflow.
# 15. We will delve into the settings of the chat environment.
# 16. We will explain agent configuration, including chat, preload, group, files and tools.
# 17. We will open the settings to show you how they can be customized.
# 18. The concept of blocks will be introduced.
# 19. Sandboxes will be explained. These are environments where you can test your conversations.
# 20. You will learn about the tools available for managing your chat environment.
# 21. Finally, we will explain the display and role display settings.
#
# We hope this tutorial helps you understand and utilize the chat environment to its full extent!
#
# Start with basic llm chat
#
# Show branching
#
# Show chats
#
# Show chat settings and explain
#
# Explain new button
#
# Add 2 other agents
#
# Explain loc_x order
#
# Explain context windows
#
# Add an input opposite dir
#
# Explain order & context
#
# Explain multiple inputs/outputs
#
# Explain output placeholders
#
# Save as entity
#
# Show agent list
#
# Open workflow entity
#
# Add workflow entity into workflow
#
# Show settings
#
# Explain agent config
#
#   Chat, preload, group
#
#   Files
#
#   Tools
#
# Open settings
#
# Explain blocks
#
# Explain sandboxes
#
# Explain tools
#
# Explain display and role display