"""Small opt-in host mode controls; shared by Windows and Apple silicon builds."""
import queue
import json
import threading
import time
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from .host_development import HostSession, Preferences, projects
from .host_tools import inventory
from .ui_components import COLORS as C, FONT, text, wrapped


class DevelopmentUI:
    def development_controls(self, parent):
        self.host_session = None
        self.development_project = tk.StringVar()
        self.development_mode = tk.StringVar(value='沙箱开发')
        self.development_hint = tk.StringVar()
        text(parent, '当前项目', size=13).pack(anchor='w', pady=(4,6))
        self.development_menu = ctk.CTkComboBox(parent, values=['主项目'], variable=self.development_project,
            command=self.select_development_project, state='readonly', height=42, font=(FONT,13),
            fg_color=C['field'], border_color=C['border'], text_color=C['text'], dropdown_fg_color=C['raised'])
        self.development_menu.pack(fill='x', pady=(0,12))
        self.inputs.append((self.development_menu, 'readonly'))
        self.development_switch = ctk.CTkSegmentedButton(parent, values=['沙箱开发', '本机开发'],
            variable=self.development_mode, command=self.change_development_mode, height=44,
            font=(FONT,14), selected_color=C['accent_surface'], selected_hover_color=C['raised'],
            unselected_color=C['field'], text_color=C['text'])
        self.development_switch.pack(fill='x', pady=(0,10))
        for button in self.development_switch._buttons_dict.values():
            tk.Frame.configure(button, takefocus=1)
            tk.Frame.bind(button, '<Return>', lambda _, b=button:b.invoke())
            tk.Frame.bind(button, '<space>', lambda _, b=button:b.invoke())
            tk.Frame.bind(button, '<FocusIn>', lambda _, b=button:b.configure(border_width=2, border_color=C['blue']))
            tk.Frame.bind(button, '<FocusOut>', lambda _, b=button:b.configure(border_width=0))
        self.inputs.append((self.development_switch, 'normal'))
        wrapped(parent, '', textvariable=self.development_hint, size=12, color=C['muted']).pack(fill='x')
        self.refresh_development_selection()

    def development_actions(self, parent):
        row = self.action_row(parent)
        self.add_action(row, '检测本机工具', self.inspect_host_tools, width=140)
        self.add_action(row, '断开本机执行', self.disconnect_host, width=140)

    def development_config(self):
        return {**{k:v.get() for k,v in self.values.items()}, 'projects':[dict(p) for p in self.project_items]}

    def refresh_development_selection(self, reset=False):
        if not hasattr(self, 'development_menu'): return
        previous = getattr(self, 'development_options', {}).get(self.development_project.get(), {}).get('id')
        prefs = Preferences(self.c.data)
        try: selected = prefs.read().get('selected', 'main') if reset or not previous else previous
        except (OSError, ValueError): selected = 'main'
        entries = projects(self.development_config())
        self.development_signature = json.dumps(entries, sort_keys=True)
        self.development_options = {p['name']+' · '+p['id']+(' [只读]' if p.get('readonly') else ''):p for p in entries}
        self.development_menu.configure(values=list(self.development_options))
        label = next((k for k,v in self.development_options.items() if v['id']==selected), next(iter(self.development_options)))
        self.development_project.set(label)
        try: mode = prefs.mode(self.development_options[label])
        except (OSError, ValueError): mode = 'sandbox'
        self.development_mode.set('本机开发' if mode=='host' else '沙箱开发')
        self.update_development_hint()

    def refresh_development_paths(self):
        if not hasattr(self, 'development_menu'): return
        signature = json.dumps(projects(self.development_config()), sort_keys=True)
        if signature != self.development_signature:
            self.disconnect_host(quiet=True)
            self.refresh_development_selection()

    def update_development_hint(self):
        host = self.development_mode.get() == '本机开发'
        connected = self.host_session and not self.host_session.closed.is_set()
        self.development_hint.set(
            ('本机已连接 · ' if connected else '尚未连接 · ')+
            '命令在本机运行，逐条确认。模型走容器代理；本机命令走本机网络。'
            if host else '文件编辑、Claude 和开发命令均在沙箱；不会启用本机执行通道。')

    def select_development_project(self, label):
        self.disconnect_host(quiet=True)
        project = self.development_options[label]
        prefs = Preferences(self.c.data)
        try:
            mode = prefs.mode(project)
            self.development_mode.set('本机开发' if mode=='host' else '沙箱开发')
            prefs.save(project, mode)
        except (OSError, ValueError) as e: self.notify(str(e), error=True)
        self.update_development_hint()

    def change_development_mode(self, label):
        self.disconnect_host(quiet=True)
        project = self.development_options[self.development_project.get()]
        try:
            Preferences(self.c.data).save(project, 'host' if label=='本机开发' else 'sandbox')
        except (OSError, ValueError) as e:
            self.development_mode.set('沙箱开发'); self.notify(str(e), error=True)
        self.update_development_hint()

    def disconnect_host(self, quiet=False):
        session = getattr(self, 'host_session', None)
        if session:
            session.close(); self.host_session = None
        if hasattr(self, 'development_hint'): self.update_development_hint()
        if not quiet: self.notify('已断开本机执行并停止本机任务。容器、项目和账号资料保留。')

    def inspect_host_tools(self):
        def report(value):
            value = json.loads(value)
            rows = [value['platform']+' / '+value['architecture'], '检测只查找程序路径，不执行项目代码。', '']
            rows += [name+'：'+(path or '未找到') for name,path in value['tools'].items()]
            rows += ['', '使用本机网络；不继承模型代理。', value['note']]
            messagebox.showinfo('本机开发工具', '\n'.join(rows), parent=self)
        self.task('检测本机工具', lambda:json.dumps(inventory(), ensure_ascii=False), report)

    def request_host_approval(self, context, cancelled):
        answer, done = [], threading.Event()
        self.messages.put(('host_approval', (context, cancelled, answer, done)))
        deadline = time.monotonic()+300
        while not cancelled.is_set() and time.monotonic()<deadline:
            if done.wait(.2): return bool(answer and answer[0])
        return False

    def show_host_approval(self, value):
        context, cancelled, answer, done = value
        if cancelled.is_set(): done.set(); return
        dialog = ctk.CTkToplevel(self)
        dialog.title('确认在本机执行 · 不是容器命令')
        dialog.geometry('740x530'); dialog.minsize(600,460)
        dialog.transient(self); dialog.attributes('-topmost', True)
        text(dialog, '本机执行确认', size=22, weight='bold').pack(anchor='w', padx=24, pady=(22,12))
        wrapped(dialog, '账号环境：'+context['environment']+'\n项目：'+context['project'], size=13).pack(fill='x', padx=24)
        wrapped(dialog, '以下命令使用本机当前用户权限和本机网络，可读取该用户有权访问的文件。项目工作目录不是系统隔离。',
                color=C['warning'], size=13).pack(fill='x', padx=24, pady=12)
        box = ctk.CTkTextbox(dialog, height=210, font=(FONT,13), wrap='word')
        box.pack(fill='both', expand=True, padx=24)
        box.insert('1.0', context['command']); box.configure(state='disabled')
        finished = False
        def finish(ok=False):
            nonlocal finished
            if finished: return
            finished = True
            answer.append(bool(ok and not cancelled.is_set())); done.set(); dialog.destroy()
        row = self.frame(dialog); row.pack(fill='x', padx=24, pady=20)
        deny = self.button(row, '拒绝', lambda:finish(False), track=False, width=110)
        deny.pack(side='left')
        self.button(row, '仅执行这一次', lambda:finish(True), primary=True, track=False, width=160).pack(side='right')
        dialog.protocol('WM_DELETE_WINDOW', finish)
        dialog.bind('<Escape>', lambda _:finish(False))
        deadline = time.monotonic()+295
        def check():
            if finished: return
            if cancelled.is_set() or time.monotonic()>deadline: finish(False)
            else: dialog.after(200, check)
        dialog.after(100, lambda:(dialog.grab_set(), deny.focus_set()))
        dialog.after(200, check)

    def prepare_development(self, config):
        """UI thread: explicit grant, captured project identity, no automatic enable."""
        chosen = self.development_options[self.development_project.get()]['id']
        project = next(p for p in projects(config) if p['id']==chosen)
        host = self.development_mode.get()=='本机开发'
        if host:
            if project.get('readonly'): raise ValueError('只读项目请选择沙箱开发。')
            if not messagebox.askyesno('启用本机开发',
                '账号环境：'+self.c.environment_name+'\n项目：'+project['path']+'\n\n'
                'Claude 和登录留在容器；本机命令使用当前系统用户权限。可能读取本机信息和凭据文件，网络不自动走容器代理。\n'
                '每条命令仍需单独确认。切换账号、项目或关闭 CCBOX 会断开并停止本机任务；不回滚已有修改。\n\n继续连接？', parent=self):
                return None
        self.disconnect_host(quiet=True)
        Preferences(self.c.data).save(project, 'host' if host else 'sandbox')
        if host:
            self.host_session = HostSession(self.c, project, self.request_host_approval)
        return project
