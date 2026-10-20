"""CCBOX Studio: native, asynchronous control panel. Backend boundaries unchanged."""
import os
import queue
import re
import secrets
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from urllib.parse import quote
import customtkinter as ctk

from .controller import Controller, LANGUAGES, ZONES, parse_proxy, validate
from .feedback import compact_notice
from .storage import docker_disk_location
from .environments import Environments
from .ui_components import COLORS as C, FONT, MONO, ActionButton, CubeMark, RouteDiagram, text, wrapped


class FormError(ValueError):
    def __init__(self, key, message):
        super().__init__(message)
        self.key = key


from .development_ui import DevelopmentUI


class App(DevelopmentUI, ctk.CTk):
    def __init__(self, controller=None):
        ctk.set_appearance_mode('dark')
        super().__init__(fg_color=C['bg'])
        self.title('CCBOX · 本地隔离工作台')
        scale = self._get_window_scaling()
        width = min(1240, int(self.winfo_screenwidth()/scale) - 60)
        height = min(900, int(self.winfo_screenheight()/scale) - 80)
        self.geometry(f'{max(980,width)}x{max(660,height)}')
        self.minsize(980, 660)
        self.c = controller or Controller()
        self.messages = queue.Queue()
        self.c.log = lambda value: self.messages.put(('log', value))
        self.busy = False
        self.loading_environment = False
        self.dirty = False
        self.buttons, self.inputs = [], []
        self.pages, self.nav, self.fields = {}, {}, {}
        self.log_visible = False
        self.disclosures = {}
        self.setup_text = {k: tk.StringVar() for k in ("project", "proxy", "hint")}
        self.current_page = '概览'
        self.data_target = tk.StringVar(value=str(self.c.data_root))
        self.storage_current = tk.StringVar(value=str(self.c.data_root))
        self.environment_label = tk.StringVar()
        self.environment_name_label = tk.StringVar()
        self.project_items = [dict(p) for p in self.c.config().get('projects', [])]
        self.project_controls = []
        self.log_count = 0
        self.suggested_zone = None
        self.proxy_test_fingerprint = None
        self.values = {k: (tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=str(v)))
                       for k, v in self.c.config().items() if k != 'projects'}
        self.proxy_vars = {k: tk.StringVar(value=v) for k,v in
                           {'scheme':'socks5h','host':'','port':'1080','username':'','password':''}.items()}
        vault_error = ''
        try:
            for k, v in self.c.credentials().get('proxy', {}).items():
                self.proxy_vars[k].set(str(v))
        except Exception:
            vault_error = '凭据解密未成功，请重新填写代理并保存。'
        self.status = tk.StringVar(value='尚未检测 Docker')
        self.task_status = tk.StringVar(value='就绪 · 等待操作')
        self.save_status = tk.StringVar(value='尚未保存配置' if not self.c.config_file.exists() else '配置已载入')
        self.proxy_status = tk.StringVar(value='尚未测试。点击「测试出口」检查当前代理连接。')
        self.proxy_badge = tk.StringVar(value='未测试')
        self.proxy_time = tk.StringVar(value='没有检测记录')
        self.check_time = tk.StringVar(value='尚未运行')
        self.url = tk.StringVar(value='https://api.ipify.org?format=json')
        self.stats = {k: tk.StringVar(value=v) for k,v in
                      {'engine':'未检测', 'proxy':'未测试', 'checks':'未运行'}.items()}
        self.render()
        self.refresh_environments()
        for var in self.values.values():
            var.trace_add('write', self.mark_dirty)
        for var in self.proxy_vars.values():
            var.trace_add('write', self.proxy_changed)
        self.show('概览')
        self.bind('<Control-s>', lambda _: self.save())
        for index, name in enumerate(self.pages, 1):
            self.bind(f'<Alt-Key-{index}>', lambda _, n=name: self.show(n))
        self._poll_id = self.after(100, self.poll)
        self.protocol('WM_DELETE_WINDOW', self.close)
        if vault_error:
            self.notify(vault_error, error=True)

    def frame(self, parent, color='transparent', **kwargs):
        return ctk.CTkFrame(parent, fg_color=color, **kwargs)

    def button(self, parent, title, command, primary=False, track=True, **kwargs):
        b = ActionButton(parent, title, command, kind='primary' if primary else 'secondary', **kwargs)
        tk.Frame.bind(b, '<FocusIn>', lambda _: self.ensure_visible(b), add='+')
        if track:
            self.buttons.append(b)
        return b

    def action_row(self, parent):
        f = self.frame(parent)
        f.pack(fill='x', pady=(16, 0))
        return f

    def add_action(self, row, title, command, primary=False, **kwargs):
        b = self.button(row, title, command, primary, **kwargs)
        b.pack(side='left', padx=(0, 10))
        return b

    def card(self, parent, title=None, description=None, color=None):
        outer = self.frame(parent, color or C['card'], corner_radius=14, border_width=1, border_color=C['divider'])
        outer.pack(fill='x', pady=(0, 16))
        inner = self.frame(outer)
        inner.pack(fill='both', expand=True, padx=24, pady=22)
        if title:
            text(inner, title, size=17, weight='bold').pack(anchor='w')
        if description:
            wrapped(inner, description).pack(fill='x', pady=(7, 14))
        return inner

    def field(self, parent, key, label, variable, choices=None, secret=False, hint=''):
        f = self.frame(parent)
        f.pack(fill='x', pady=(0, 14))
        text(f, label, size=13).pack(anchor='w', pady=(0, 7))
        row = self.frame(f)
        row.pack(fill='x')
        common = dict(height=42, corner_radius=8, fg_color=C['field'], border_color=C['border'],
                      text_color=C['text'], font=(FONT, 13))
        if choices:
            widget = ctk.CTkComboBox(row, values=choices, variable=variable, state='readonly', dropdown_fg_color=C['raised'],
                                   dropdown_text_color=C['text'], dropdown_hover_color='#344941',
                                   button_color=C['raised'], button_hover_color='#344941', **common)
            normal_state = 'readonly'
        else:
            widget = ctk.CTkEntry(row, textvariable=variable, show='●' if secret else '', **common)
            normal_state = 'normal'
        widget.pack(side='left', fill='x', expand=True)
        if secret:
            reveal = self.button(row, '显示', lambda: self.toggle_password(widget, reveal), width=68)
            reveal.pack(side='right', padx=(8,0))
        error = wrapped(f, '', color=C['danger'], size=12)
        if hint:
            wrapped(f, hint, size=12, color=C['subtle']).pack(fill='x', pady=(6,0))
        self.fields[key] = {'widget':widget, 'error':error, 'page':self.building_page}
        self.inputs.append((widget, normal_state))
        widget.bind('<FocusIn>', lambda _: self.focus_field(key), add='+')
        widget.bind('<FocusOut>', lambda _: widget.configure(border_color=C['danger'] if error.winfo_manager() else C['border']), add='+')
        variable.trace_add('write', lambda *_: self.clear_field_error(key))
        return widget

    def pair(self, parent, weights=(1,1)):
        row = self.frame(parent)
        row.pack(fill='x')
        frames = []
        for i, weight in enumerate(weights):
            row.grid_columnconfigure(i, weight=weight, uniform='pair' if len(set(weights))==1 else '')
            f = self.frame(row)
            f.grid(row=0, column=i, sticky='nsew', padx=(0 if i==0 else 8,0 if i==len(weights)-1 else 8))
            frames.append(f)
        return frames

    def render(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        side = self.frame(self, C['sidebar'], width=210, corner_radius=0)
        side.grid(row=0,column=0,sticky='nsew')
        side.grid_propagate(False)
        side.grid_columnconfigure(0,weight=1)
        side.grid_rowconfigure(3,weight=1)
        brand = self.frame(side)
        brand.grid(row=0,column=0,sticky='ew',padx=22,pady=(32,32))
        CubeMark(brand, 36).pack(side='left',padx=(0,10))
        wordmark = self.frame(brand)
        wordmark.pack(side='left')
        text(wordmark,'CCBOX',size=23,weight='bold').pack(anchor='w')
        text(wordmark,'独立开发空间',size=9,color=C['subtle']).pack(anchor='w')
        text(side,'工作空间',size=11,color=C['subtle']).grid(row=1,column=0,sticky='w',padx=26,pady=(0,12))
        nav = self.frame(side)
        nav.grid(row=2,column=0,sticky='ew',padx=14)
        for i,(key,title) in enumerate([('概览','开始使用'),('环境配置','项目文件'),('网络连接','代理设置'),('隔离检查','检查与修复')],1):
            b = self.button(nav, f'0{i}    {title}', lambda n=key:self.show(n), track=False, anchor='w', width=180)
            b.configure(fg_color=C['sidebar'], border_color=C['sidebar'], hover_color=C['raised'])
            b.base_border = C['sidebar']
            b.pack(fill='x',pady=4)
            self.nav[key]=b
        note = self.frame(side,C['card'],corner_radius=12,border_width=1,border_color=C['divider'])
        note.grid(row=4,column=0,sticky='ew',padx=16,pady=16)
        text(note,'本地优先',size=13,weight='bold').pack(anchor='w',padx=14,pady=(14,5))
        text(note,'独立资料 · 显式代理\n隔离效果以检测为准',size=11,color=C['muted']).pack(anchor='w',padx=14,pady=(0,14))
        text(side,'桌面版  /  0.3.0',size=10,color=C['subtle']).grid(row=5,column=0,sticky='w',padx=26,pady=(0,24))
        main = self.frame(self)
        main.grid(row=0,column=1,sticky='nsew',padx=(28,24),pady=(20,16))
        main.grid_columnconfigure(0,weight=1)
        main.grid_rowconfigure(3,weight=1)
        top = self.frame(main)
        top.grid(row=0,column=0,sticky='ew')
        self.breadcrumb = text(top,'WORKSPACE / OVERVIEW',size=11,color=C['subtle'])
        self.breadcrumb.pack(side='left')
        self.button(top,'新手指南',self.quick_help,track=False,width=112).pack(side='right')
        heading=self.frame(main)
        heading.grid(row=1,column=0,sticky='ew',pady=(14,12))
        self.header=text(heading,'',size=29,weight='bold')
        self.header.pack(anchor='w')
        self.subtitle=wrapped(heading,'',size=13)
        self.subtitle.pack(fill='x',pady=(6,0))
        switcher=self.frame(heading,C['card'],corner_radius=10)
        switcher.pack(fill='x',pady=(12,0))
        text(switcher,'当前独立空间',size=12,color=C['muted']).pack(side='left',padx=(14,10))
        self.environment_menu=ctk.CTkComboBox(switcher,variable=self.environment_label,values=['默认环境'],
            command=self.switch_environment,state='readonly',height=40,width=230,font=(FONT,13),
            fg_color=C['field'],border_color=C['border'],text_color=C['text'],dropdown_fg_color=C['raised'])
        self.environment_menu.pack(side='left',fill='x',expand=True,pady=10)
        self.inputs.append((self.environment_menu,'readonly'))
        self.button(switcher,'新建',self.create_environment,width=70).pack(side='left',padx=8)
        self.button(switcher,'重命名',self.rename_environment,width=82).pack(side='left',padx=(0,10))
        self.banner=self.frame(main,C['accent_surface'],corner_radius=9)
        self.banner.grid(row=2,column=0,sticky='ew',pady=(0,12))
        self.banner.grid_columnconfigure(0,weight=1)
        notice_body=self.frame(self.banner)
        notice_body.grid(row=0,column=0,sticky='ew',padx=14,pady=9)
        self.banner_label=wrapped(notice_body,'')
        self.banner_label.pack(fill='x')
        notice_actions=self.frame(self.banner)
        notice_actions.grid(row=0,column=1,padx=(0,8),pady=6)
        self.button(notice_actions,'查看记录',self.reveal_log,track=False,width=82).pack(side='left',padx=(0,6))
        self.button(notice_actions,'收起',lambda:self.banner.grid_remove(),track=False,width=64).pack(side='left')
        self.banner.grid_remove()
        self.scroller=ctk.CTkScrollableFrame(main,fg_color='transparent',corner_radius=0,
            scrollbar_button_color=C['border'],scrollbar_button_hover_color=C['subtle'])
        self.scroller.grid(row=3,column=0,sticky='nsew',padx=(0,0))
        self.scroller.grid_columnconfigure(0,weight=1)
        self.canvas=self.scroller._parent_canvas
        self.holder=self.scroller
        for name in self.nav:
            self.pages[name]=self.frame(self.scroller)
            self.pages[name].grid(row=0,column=0,sticky='nsew',padx=0)
        for name, builder in [('概览',self.overview),('网络连接',self.network),('环境配置',self.environment),('隔离检查',self.checks)]:
            self.building_page=name
            builder()
        self.bottom=self.frame(main,C['card'],corner_radius=12,border_width=1,border_color=C['divider'])
        self.bottom.grid(row=4,column=0,sticky='ew',pady=(14,10))
        save_info=self.frame(self.bottom)
        save_info.pack(side='left',fill='x',expand=True,padx=18,pady=12)
        self.saved_label=text(save_info,'',size=13,textvariable=self.save_status)
        self.saved_label.pack(anchor='w')
        text(save_info,'保存后，首页「开始开发」会引导应用更改',size=11,color=C['subtle']).pack(anchor='w')
        self.save_button=self.button(self.bottom,'保存配置  ·  Ctrl+S',self.save,width=166)
        self.save_button.pack(side='right',padx=14,pady=12)
        footer=self.frame(main)
        footer.grid(row=5,column=0,sticky='ew')
        self.task_label=text(footer,'',size=12,color=C['muted'],textvariable=self.task_status)
        self.task_label.pack(side='left')
        self.log_toggle=self.button(footer,'操作记录  0  ↑',self.toggle_log,track=False,width=144)
        self.log_toggle.configure(height=32)
        self.log_toggle.pack(side='right')
        self.progress=ctk.CTkProgressBar(main,height=3,fg_color=C['divider'],progress_color=C['accent'],mode='indeterminate')
        self.progress.grid(row=6,column=0,sticky='ew',pady=(8,0))
        self.progress.set(0)
        self.log=ctk.CTkTextbox(main,height=128,corner_radius=9,fg_color=C['sidebar'],text_color=C['muted'],
                               font=(MONO,12),wrap='word',border_width=1,border_color=C['divider'],state='disabled')
        self.log.grid(row=7,column=0,sticky='ew',pady=(10,0))
        self.log.grid_remove()

    def disclosure(self, parent, key, title, hint=''):
        outer=self.frame(parent,C['card'],corner_radius=12,border_width=1,border_color=C['divider'])
        outer.pack(fill='x',pady=(0,14))
        head=self.button(outer,'展开  ·  '+title,lambda:self.toggle_section(key),track=False,anchor='w')
        head.pack(fill='x',padx=14,pady=12)
        if hint:
            wrapped(outer,hint,size=12).pack(fill='x',padx=20,pady=(0,14))
        body=self.frame(outer)
        self.disclosures[key]={'body':body,'button':head,'title':title,'open':False}
        return body

    def toggle_section(self,key,opened=None):
        section=self.disclosures[key]
        state=not section['open'] if opened is None else opened
        section['open']=state
        section['button'].configure(text=('收起' if state else '展开')+'  ·  '+section['title'])
        if state:
            section['body'].pack(fill='x',padx=20,pady=(0,20))
        else:
            section['body'].pack_forget()
            section['button'].focus_set()

    def reveal_field(self,key):
        widget=self.fields[key]['widget']
        for section in self.disclosures.values():
            node=widget
            while node is not None:
                if node is section['body']:
                    self.toggle_section(next(k for k,v in self.disclosures.items() if v is section),True)
                    break
                node=getattr(node,'master',None)

    def overview(self):
        p=self.pages['概览']
        editor=self.card(p,color=C['hero'])
        text(editor,'在独立空间里，开始今天的开发',size=21,weight='bold').pack(anchor='w')
        wrapped(editor,'选择账号空间和项目，再选择开发方式。已有登录与项目配置继续保留。',size=13).pack(fill='x',pady=(8,12))
        self.development_controls(editor)
        wrapped(editor,'',textvariable=self.setup_text['hint'],size=13,color=C['accent']).pack(fill='x')
        r=self.action_row(editor)
        self.sandbox_editor_button=self.add_action(r,'打开 / 恢复 VS Code  →',self.start_development,True,width=250)
        self.add_action(r,'第一次怎么用？',self.quick_help,width=145)
        self.development_actions(editor)
        wrapped(editor,'项目和登录资料会保留。本机开发需保持 CCBOX 打开，关闭会断开并停止本机任务。',size=12).pack(fill='x',pady=(12,0))
        f=self.card(p,'开始前，只需两项设置')
        for index,key,title,command in [(1,'project','选择项目',lambda:self.show('环境配置')),
                                        (2,'proxy','填写代理',lambda:self.show('网络连接'))]:
            row=self.frame(f)
            row.pack(fill='x',pady=(10,4))
            text(row,f'{index:02}',size=20,color=C['accent']).pack(side='left',padx=(0,16))
            self.button(row,'设置',command,width=72).pack(side='right',padx=(12,0))
            body=self.frame(row)
            body.pack(side='left',fill='x',expand=True)
            text(body,title,size=14,weight='bold').pack(anchor='w')
            wrapped(body,'',textvariable=self.setup_text[key],size=12).pack(fill='x',pady=(3,0))
        wrapped(f,'上方可切换或新建独立空间；每个空间分别登录自己的 Claude 账号。',size=12).pack(fill='x',pady=(12,0))
        more=self.disclosure(p,'home_tools','更多打开方式与停止','日常开发只需上面的「打开 / 恢复 VS Code」。以下入口按需使用。')
        for title,desc,command in [
            ('打开 Claude 终端','在当前容器中使用命令行；首次可在这里登录。',lambda:self.task('打开 CLI',self.c.terminal)),
            ('打开沙箱浏览器','网页和授权在容器浏览器中打开。',lambda:self.task('打开浏览器',lambda:self.c.open_url('file:///opt/ccbox/browser-check.html'))),
            ('连接本机 VS Code','兼容入口：编辑器界面在 Windows，后端在容器。',lambda:self.task('连接 VS Code',self.c.open_vscode)),
            ('停止当前空间','停止浏览器和终端；保留项目、配置与登录资料。',self.stop_environment)]:
            row=self.frame(more)
            row.pack(fill='x',pady=(8,8))
            self.button(row,title,command,width=168).pack(side='left',padx=(0,16))
            wrapped(row,desc,size=12).pack(side='left',fill='x',expand=True)
        wrapped(p,'隔离范围以「检查与修复」中的实测为准，不代表账号或匿名性保证。',size=11).pack(fill='x',pady=(0,10))

    def refresh_setup(self):
        if not hasattr(self,'sandbox_editor_button'):
            return
        self.refresh_development_paths()
        self.update_development_hint()
        from .guided_start import next_step
        config={k:v.get() for k,v in self.values.items()}
        proxy={k:v.get() for k,v in self.proxy_vars.items()}
        step=next_step(config,proxy)
        path=config.get('workspace','').strip()
        self.setup_text['project'].set(f'已选择 {1+len(self.project_items)} 个文件夹 · '+Path(path).name if path else '未选择 · 只共享你允许访问的项目文件夹')
        self.setup_text['proxy'].set('已填写 · '+self.stats['proxy'].get()+'（不等于隔离验证）' if proxy['host'].strip() else '未填写 · 支持 SOCKS5、HTTP 和 HTTPS')
        label,hint={
            'project':('先选择项目  →','从你的电脑里选择一个项目文件夹。'),
            'proxy':('下一步：填写代理  →','项目已选择，再填写当前空间使用的代理。'),
            'open':('保存并打开 VS Code  →' if self.dirty else '打开 / 恢复 VS Code  →','打开沙箱里的 VS Code；已最小化的窗口也会恢复。首次需要下载组件。')
        }[step]
        self.sandbox_editor_button.configure(text=label)
        self.setup_text['hint'].set(hint)
        if hasattr(self,'bottom'):
            if self.current_page=='概览' and not self.dirty:
                self.bottom.grid_remove()
            else:
                self.bottom.grid()

    def start_development(self):
        if self.busy:
            return
        from .guided_start import next_step, runtime_changed, open_workspace
        from .sandbox_editor import ready
        step=next_step({k:v.get() for k,v in self.values.items()},{k:v.get() for k,v in self.proxy_vars.items()})
        if step!='open':
            key='workspace' if step=='project' else 'proxy.host'
            self.show(self.fields[key]['page'])
            self.fields[key]['widget'].focus_set()
            return
        try:
            config,proxy=self.collect_config(),self.collect_proxy()
            restart=runtime_changed(self.c,config,proxy)
        except ValueError as e:
            self.form_error(e)
            return
        except Exception:
            self.notify('读取已应用设置失败。请到「检查与修复」查看状态，现有资料未改动。',error=True)
            return
        first=not ready(self.c)
        if first or restart:
            details=('首次会下载组件；已有桌面的工具栏升级仅短暂重连网关。旧环境缺少桌面依赖时可能需要重启。\n' if first else '')
            details+=('检测到新设置，需要重启当前空间。请先保存编辑器中的工作。\n' if restart else '')
            details+='项目和登录资料保留；请等当前模型请求完成后继续。其他独立空间不受影响。\n'
            if first:
                details+='编辑器使用 Docker 隔离；兼容模式下不启用 Electron 内层沙箱。\n'
            if not messagebox.askyesno('准备开始开发',details+'\n准备好后继续？',parent=self):
                return
        try:
            project=self.prepare_development(config)
        except (OSError, ValueError) as e:
            self.notify(str(e),error=True)
            return
        if project is None:
            return
        save=self.dirty or not self.c.config_file.exists()
        controller=self.c
        session=self.host_session
        def open_selected():
            try:
                result=open_workspace(controller,config,proxy,save=save,restart=restart,upgrade=first,
                    on_saved=lambda:self.messages.put(('configuration_saved',None)),
                    before_launch=session.start if session else None, selected_workspace=project['remote'])
                if session:
                    result+='\n本机开发已连接。请保持 CCBOX 打开；Claude 通过 ccbox-host 工具运行本机命令。已有 Claude 会话请新建会话或重新连接 MCP。'
                return result
            except Exception:
                if session: session.close()
                raise
        self.task('开始开发',open_selected,self.development_opened)

    def development_opened(self,result):
        self.stats['engine'].set('已启动')
        self.update_development_hint()
        self.refresh_setup()

    def stop_environment(self):
        if self.busy:
            return
        if messagebox.askyesno('停止当前独立空间','会关闭当前空间的浏览器、编辑器和终端。请先保存工作。\n项目和账号登录资料保留；其他空间继续运行。\n\n确定停止？',parent=self):
            self.disconnect_host(quiet=True)
            self.task('停止环境',self.c.stop,self.stopped)

    def quick_help(self):
        messagebox.showinfo('第一次使用 · 三步开始',
            '1. 选择项目\n在「项目文件」添加文件夹，可跨磁盘添加多个项目。\n\n'
            '2. 填写代理\n在「代理设置」粘贴连接信息，测试后核对结果。\n\n'
            '3. 开始开发\n回到首页，点击「打开 / 恢复 VS Code」。自动保存、准备并打开沙箱 VS Code。\n'
            '首次请自行确认项目是否可信，再打开 Claude 面板登录。已有容器登录可继续使用。\n\n'
            '切换账号：上方切换独立空间，再点「打开 / 恢复 VS Code」。已打开的窗口仍属于原空间。\n'
            '找回窗口：页面顶部点「恢复 VS Code」；窗口已关闭时，回 CCBOX 点「打开 / 恢复 VS Code」。\n'
            '结束使用：直接关闭窗口会保留运行；要节省资源，在首页「更多打开方式与停止」中停止。',parent=self)

    def network(self):
        p=self.pages['网络连接']
        f=self.card(p,'填写代理连接','把供应商提供的代理信息粘贴进来，或在下方手动填写。每个独立空间单独保存。')
        r=self.frame(f)
        r.pack(fill='x',pady=(0,18))
        self.button(r,'从剪贴板导入',self.paste_proxy,width=148).pack(side='left')
        wrapped(r,'支持 URL 或 用户名:密码@地址:端口',size=11).pack(side='left',fill='x',expand=True,padx=16)
        self.field(f,'proxy.scheme','代理类型',self.proxy_vars['scheme'],['socks5h','http','https'],hint='SOCKS5 请选择 socks5h：网站域名由代理端解析。')
        a,b=self.pair(f,(3,1))
        self.field(a,'proxy.host','服务器地址',self.proxy_vars['host'])
        self.field(b,'proxy.port','端口',self.proxy_vars['port'])
        self.field(f,'proxy.username','用户名',self.proxy_vars['username'],hint='无需认证的代理可留空用户名和密码。')
        self.field(f,'proxy.password','密码',self.proxy_vars['password'],secret=True)
        wrapped(f,'SOCKS5 使用远端目标 DNS。凭据保存时由 Windows 当前用户 DPAPI 加密。',size=11).pack(fill='x')
        f=self.card(p,'测试代理是否可用')
        bar=self.frame(f)
        bar.pack(fill='x',pady=(8,14))
        self.proxy_badge_label=text(bar,'',size=12,color=C['warning'],textvariable=self.proxy_badge)
        self.proxy_badge_label.pack(side='left')
        text(bar,'',size=11,color=C['subtle'],textvariable=self.proxy_time).pack(side='right')
        wrapped(f,'',textvariable=self.proxy_status).pack(fill='x')
        r=self.action_row(f)
        self.add_action(r,'测试代理  →',self.test_proxy,True,width=142)
        self.zone_button=self.add_action(r,'采用检测时区',self.apply_detected_zone,width=134)
        self.zone_button.configure(state='disabled')
        f=self.disclosure(p,'network_details','连接说明与注意事项')
        wrapped(f,'代理连通 ≠ 容器隔离验证通过。代理服务器自身域名仍由宿主网络解析。\n'
                '普通 HTTP / SOCKS5 上游认证不加密；需要加密认证时，选择供应商支持的 HTTPS 代理。',size=12).pack(fill='x',pady=(8,0))

    def environment(self):
        p=self.pages['环境配置']
        f=self.card(p,'选择你的主项目','选择需要开发的文件夹即可。文件直接共享到沙箱，保存修改也会同步到原文件。')
        wrapped(f,'',textvariable=self.environment_name_label,size=12,color=C['accent']).pack(fill='x',pady=(0,12))
        self.field(f,'workspace','项目文件夹',self.values['workspace'],hint='不要选择整块磁盘、用户目录或包含私人凭据的目录。可写挂载会直接修改宿主机项目。')
        r=self.frame(f)
        r.pack(fill='x')
        self.button(r,'选择文件夹…',self.choose_folder,width=140).pack(side='left')
        switch=ctk.CTkSwitch(r,text='只读（禁止修改）',variable=self.values['readonly'],font=(FONT,13),
                            text_color=C['text'],progress_color=C['accent'],button_color=C['text'],button_hover_color=C['accent'])
        switch.pack(side='right',padx=6)
        self.inputs.append((switch,'normal'))
        f=self.card(p,'还有其他项目？','继续添加文件夹，支持不同磁盘。开始开发时会一起打开，不用手动配置 VS Code。')
        self.project_list=self.frame(f)
        self.project_list.pack(fill='x',pady=(4,4))
        self.render_projects()
        self.add_action(self.action_row(f),'添加项目文件夹…',self.add_project,width=170)
        wrapped(f,'同一个环境内的 Claude 可访问所有已挂载项目，单独打开不减少访问权限。不同账号挂载同一目录时文件仍然共享；需独立副本时请选择不同目录。',size=12,color=C['warning']).pack(fill='x',pady=(14,0))
        self.add_action(self.action_row(p),'返回首页开始开发  →',lambda:self.show('概览'),True,width=200)
        text(p,'不确定的设置保持默认即可',size=13,color=C['subtle']).pack(anchor='w',pady=(24,12))
        f=self.disclosure(p,'storage','数据存储与迁移','工具配置与 Docker 磁盘分开管理。迁移会先复制、校验，再切换位置；原数据目录保留为备份。')
        wrapped(f,'当前工具数据目录',size=12,color=C['subtle']).pack(fill='x')
        wrapped(f,'',textvariable=self.storage_current,size=12).pack(fill='x',pady=(4,14))
        self.field(f,'data_dir','新数据目录',self.data_target,hint='选择独立的空文件夹，例如 D:\\CCBOX\\data。先关闭 CCBOX 专用 VS Code 窗口，再点迁移。')
        r=self.action_row(f)
        self.add_action(r,'选择数据目录…',self.choose_data_folder,width=145)
        self.add_action(r,'校验并迁移',self.migrate_storage,width=120)
        wrapped(f,'Docker 镜像 / 登录卷磁盘：'+docker_disk_location(),size=12).pack(fill='x',pady=(16,5))
        wrapped(f,'Docker 磁盘位置由 Docker Desktop → Settings → Resources → Advanced 管理，影响全部 Docker 项目。不要移动正在使用的 VHDX 文件。',size=11).pack(fill='x')
        f=self.disclosure(p,'local_editor','本机 VS Code 连接设置（可选）','与项目目录分开保存，支持选择 D 盘或其他位置。只改变连接配置，不迁移项目、登录资料或 Docker 镜像。')
        self.field(f,'vscode_dir','连接配置目录',self.values['vscode_dir'],
                   hint='留空存入当前环境的数据目录。新环境会自动追加独立环境 ID 子目录，即使选择同一基础位置也不共用编辑器资料。')
        r=self.action_row(f)
        self.add_action(r,'选择配置文件夹…',self.choose_vscode_folder,width=154)
        self.add_action(r,'恢复默认',lambda:self.values['vscode_dir'].set(''),width=104)
        wrapped(f,'操作顺序：选择目录 → 保存配置 → 首页「更多打开方式与停止」→「连接本机 VS Code」。首次自动安装 Dev Containers；CCBOX 专用 VS Code 配置与缓存也放在这里，不混用宿主机的登录资料。',size=12).pack(fill='x',pady=(14,0))
        r=self.action_row(f)
        self.add_action(r,'检查编辑器连接',lambda:self.task('检查编辑器',self.c.editor_status),width=145)
        self.add_action(r,'导出手动连接配置',lambda:self.task('生成 VS Code 配置',self.c.devcontainer),width=160)
        f=self.disclosure(p,'region','语言与时区','独立设置容器与浏览器语言、时区，不改动 Windows。IP 地理数据库结果仅供参考。')
        self.field(f,'timezone','IANA 时区',self.values['timezone'],ZONES)
        self.field(f,'locale','系统 / 浏览器语言',self.values['locale'],list(LANGUAGES))
        f=self.disclosure(p,'resources','CPU 与内存','为工作容器预留合适的计算资源。默认 2 核 CPU / 4 GB 内存。')
        a,b=self.pair(f)
        self.field(a,'cpus','CPU 核数',self.values['cpus'])
        self.field(b,'memory_gb','内存 / GB',self.values['memory_gb'])
        f=self.disclosure(p,'advanced','端口与版本（高级）','CLI 版本更新需要重新构建。首次镜像构建使用 Docker 自身网络。')
        a,b=self.pair(f)
        self.field(a,'desktop_port','桌面本地端口',self.values['desktop_port'])
        self.field(b,'claude_version','Claude Code 版本',self.values['claude_version'],hint='latest 或指定 x.y.z')

    def checks(self):
        p=self.pages['隔离检查']
        maintenance=self.card(p,'遇到问题，先检查这里','先确认 Docker 已打开，再检查当前空间。检查不会发送 Claude 模型消息。')
        r=self.action_row(maintenance)
        self.add_action(r,'检查 Docker',lambda:self.task('检测依赖',self.c.doctor,self.doctor_done),width=130)
        self.add_action(r,'刷新运行状态',lambda:self.task('读取状态',self.c.state,self.runtime_done),width=140)
        self.add_action(r,'检查编辑器',lambda:self.task('检查沙箱编辑器',self.c.sandbox_editor_status),width=125)
        wrapped(maintenance,'',textvariable=self.status,size=12).pack(fill='x',pady=(12,0))
        f=self.card(p,'网络与隔离检查','检查真实网络边界，而不是生成“防封分数”。请先启动容器；每一项结果单独判定。')
        r=self.action_row(f)
        self.add_action(r,'运行检查  →',lambda:self.task('隔离检查',self.c.audit,self.show_report),True,width=142)
        text(f,'',size=11,color=C['subtle'],textvariable=self.check_time).pack(anchor='w',pady=(16,8))
        self.check_list=self.frame(f,C['field'],corner_radius=10)
        self.check_list.pack(fill='x')
        self.check_rows=[]
        self.render_checks([
            ('外部网卡与权限','未检测','确认仅有 lo、运行用户不是 root'),
            ('DNS 与直连出站','未检测','检查 IPv4 / IPv6、UDP 和 DNS 配置'),
            ('代理出口','未检测','读取当前出口 IP'),
            ('浏览器环境','待手动核对','在沙箱浏览器确认 WebRTC、语言和时区')])
        f=self.card(p,'浏览器实测 / 授权链接','网页在容器 Firefox 内运行，宿主浏览器只显示远程画面。')
        self.field(f,'url','网页地址',self.url)
        self.add_action(self.action_row(f),'在沙箱中打开  ↗',self.visit,width=178)
        f=self.disclosure(p,'check_details','查看完整检测输出')
        self.report=ctk.CTkTextbox(f,height=190,font=(MONO,12),fg_color=C['field'],text_color=C['muted'],wrap='word',corner_radius=8)
        self.report.pack(fill='x',pady=(12,0))
        self.report.insert('end','尚未运行。请先启动环境。\n浏览器层结果需要在沙箱浏览器页面核对。')
        self.report.configure(state='disabled')
        f=self.disclosure(p,'repair','高级维护与修复','仅排查问题时使用。会中断工作的操作需确认后才执行。')
        r=self.action_row(f)
        self.add_action(r,'准备 / 修复编辑器',lambda:self.open_sandbox_editor(repair=True),width=155)
        self.add_action(r,'重启并应用设置',self.restart_environment,width=150)
        r=self.action_row(f)
        self.add_action(r,'模拟代理中断',self.outage,width=134)
        self.add_action(r,'重新构建组件',self.rebuild_environment,width=134)
        wrapped(f,'停止与重启保留资料。重新构建更新组件，不会重置账号；首次下载使用 Docker 的网络。',size=12).pack(fill='x',pady=(14,0))

    def restart_environment(self):
        if self.busy:
            return
        if self.dirty:
            self.notify('请先保存修改，再重启当前空间。',error=True)
            return
        if messagebox.askyesno('重启当前空间','请保存编辑器中的工作。当前浏览器和终端将关闭，项目和登录资料保留。继续？',parent=self):
            def restart():
                if self.c.file.exists():
                    self.c.stop()
                return self.c.start()
            self.task('重启环境',restart,self.started)

    def rebuild_environment(self):
        if self.busy:
            return
        if self.dirty:
            self.notify('请先保存修改，再重新构建组件。',error=True)
            return
        if messagebox.askyesno('重新构建组件','将停止当前空间并重新准备组件，然后启动。请先保存工作。项目和登录资料保留。继续？',parent=self):
            def rebuild():
                if self.c.file.exists():
                    self.c.stop()
                self.c.build()
                return self.c.start()
            self.task('构建镜像',rebuild,self.started)

    def render_checks(self, rows):
        for child in self.check_list.winfo_children():
            child.destroy()
        self.check_rows=list(rows)
        for i,(name,status,detail) in enumerate(rows):
            if i:
                self.frame(self.check_list,C['divider'],height=1,corner_radius=0).pack(fill='x',padx=16)
            row=self.frame(self.check_list)
            row.pack(fill='x',padx=16,pady=12)
            top=self.frame(row)
            top.pack(fill='x')
            text(top,name,size=13).pack(side='left')
            color=C['accent'] if status=='PASS' else C['danger'] if status=='FAIL' else C['warning']
            text(top,status,size=11,color=color).pack(side='right')
            wrapped(row,detail,size=11,color=C['subtle']).pack(fill='x',pady=(3,0))

    def show(self,name):
        for p in self.pages.values():
            p.grid_remove()
        self.pages[name].grid()
        self.current_page=name
        headings={
            '概览':('开始使用','选好项目和代理，接下来交给 CCBOX。','首页'),
            '网络连接':('代理设置','每个独立空间使用自己的代理。测试连通后，再回首页开始开发。','网络'),
            '环境配置':('项目文件','只共享你选中的文件夹；其他设置按需展开。','项目'),
            '隔离检查':('检查与修复','看懂状态、找到问题，再决定是否修复。','帮助')}
        title,subtitle,tag=headings[name]
        self.header.configure(text=title)
        self.subtitle.configure(text=subtitle)
        self.breadcrumb.configure(text=f'CCBOX  /  {tag}')
        for key,b in self.nav.items():
            selected=key==name
            b.base_border=C['hero_border'] if selected else C['sidebar']
            b.configure(fg_color=C['accent_surface'] if selected else C['sidebar'],
                        border_color=b.base_border,text_color=C['accent'] if selected else C['muted'])
        self.update_idletasks()
        self.canvas.yview_moveto(0)
        self.refresh_setup()

    def ensure_visible(self,widget):
        if not hasattr(self,'canvas'):
            return
        ancestor=widget
        while ancestor is not self.scroller:
            ancestor=getattr(ancestor,'master',None)
            if ancestor is None:
                return
        self.update_idletasks()
        y=widget.winfo_rooty()-self.scroller.winfo_rooty()
        top,height=self.canvas.canvasy(0),self.canvas.winfo_height()
        total=max(1,self.scroller.winfo_height())
        if y<top:
            self.canvas.yview_moveto(max(0,y-14)/total)
        elif y+widget.winfo_height()>top+height:
            self.canvas.yview_moveto((y+widget.winfo_height()+14-height)/total)

    def focus_field(self,key):
        field=self.fields[key]
        field['widget'].configure(border_color=C['blue'])
        self.ensure_visible(field['widget'])

    def clear_field_error(self,key):
        if key in self.fields:
            f=self.fields[key]
            f['error'].pack_forget()
            f['widget'].configure(border_color=C['border'])

    def form_error(self,error):
        field=self.fields.get(getattr(error,'key',''))
        self.notify(str(error),error=True)
        if field:
            self.show(field['page'])
            self.reveal_field(error.key)
            field['error'].configure(text=str(error))
            field['error'].pack(fill='x',pady=(6,0))
            field['widget'].configure(border_color=C['danger'])
            field['widget'].focus_set()
            self.ensure_visible(field['widget'])

    def mark_dirty(self,*_):
        if self.loading_environment:
            return
        self.dirty=True
        self.save_status.set('有修改尚未保存')
        self.saved_label.configure(text_color=C['warning'])
        self.refresh_setup()

    def proxy_changed(self,*_):
        if self.loading_environment:
            return
        self.mark_dirty()
        self.suggested_zone=None
        self.proxy_test_fingerprint=None
        self.stats['proxy'].set('未测试')
        self.proxy_badge.set('设置已更改 · 需要重测')
        self.proxy_badge_label.configure(text_color=C['warning'])
        self.proxy_status.set('代理字段已修改，上一次测试结果不适用于当前配置。')
        self.proxy_time.set('等待重新测试')
        self.zone_button.configure(state='disabled')
        self.refresh_setup()

    def toggle_password(self,widget,button):
        hidden=bool(widget.cget('show'))
        widget.configure(show='' if hidden else '●')
        button.configure(text='隐藏' if hidden else '显示')

    def choose_folder(self):
        p=filedialog.askdirectory(title='选择共享到容器的项目目录',parent=self)
        if p:
            self.values['workspace'].set(p)

    def refresh_environments(self):
        catalog=Environments(self.c.root).read()
        self.environment_options={i['name']:i['id'] for i in catalog['items']}
        self.environment_menu.configure(values=list(self.environment_options))
        name=self.c.environment_name
        self.environment_label.set(name)
        self.environment_name_label.set(f'操作对象：{name}  ·  {self.c.environment_id}')
        self.title(f'CCBOX · {name} · 本地隔离工作台')

    def can_change_environment(self):
        if self.busy:
            return False
        if self.dirty and not messagebox.askyesno('切换账号环境','当前有未保存修改。放弃这些修改并继续？\n运行中的容器和登录资料不变。',parent=self):
            return False
        return True

    def load_environment(self,key):
        new=Controller(self.c.root,key)
        config=new.config()
        # Fail closed: keep the current UI/context if a vault is unreadable.
        proxy=new.credentials().get('proxy',{})
        Environments(self.c.root).select(key)
        self.disconnect_host(quiet=True)
        self.loading_environment=True
        try:
            self.c=new
            self.c.log=lambda value:self.messages.put(('log',f'[{new.environment_name}] {value}'))
            for k,var in self.values.items():
                var.set(config[k])
            defaults={'scheme':'socks5h','host':'','port':'1080','username':'','password':''}
            for k,var in self.proxy_vars.items():
                var.set(proxy.get(k,defaults[k]))
            self.project_items=[dict(p) for p in config.get('projects',[])]
            self.render_projects()
            self.refresh_development_selection(reset=True)
            for k in self.fields:
                self.clear_field_error(k)
            self.proxy_test_fingerprint=None
            self.suggested_zone=None
            self.stats['engine'].set('待刷新')
            self.stats['proxy'].set('未测试')
            self.stats['checks'].set('未运行')
            self.status.set('请刷新当前环境状态')
            self.proxy_status.set('已切换环境，请针对本环境测试代理。')
            self.proxy_badge.set('未测试')
            self.proxy_badge_label.configure(text_color=C['warning'])
            self.proxy_time.set('没有检测记录')
            self.zone_button.configure(state='disabled')
            self.check_time.set('尚未运行')
            self.report.configure(state='normal')
            self.report.delete('1.0','end')
            self.report.insert('end','当前环境尚未检测。其他环境的检测结果不适用于此环境。')
            self.report.configure(state='disabled')
            self.render_checks([('当前环境','未检测','请启动后运行检查')])
            self.url.set('https://api.ipify.org?format=json')
            self.storage_current.set(str(self.c.data_root))
            self.data_target.set(str(self.c.data_root))
            self.refresh_environments()
            self.saved('')
            self.notify(f'已选择「{self.c.environment_name}」。其他容器与已打开的 VS Code 窗口保持原环境；点击一键连接打开本环境。')
            self.task_status.set('当前环境：'+self.c.environment_name)
        finally:
            self.loading_environment=False

    def switch_environment(self,label):
        if self.environment_options.get(label)==self.c.environment_id:
            return
        if not self.can_change_environment():
            self.refresh_environments()
            return
        try:
            self.load_environment(self.environment_options[label])
        except Exception as e:
            self.refresh_environments()
            self.notify(str(e),error=True)

    def create_environment(self):
        if not self.can_change_environment():
            return
        name=simpledialog.askstring('新建独立环境','环境名称（例如：工作账号 B）\n新环境不复制任何登录、代理或项目配置。',parent=self)
        if name is None:
            return
        try:
            key=Environments(self.c.root).create(name)
            self.load_environment(key)
            self.show('网络连接')
            self.notify('新环境已创建。先配置代理与项目并保存，再启动、登录；原环境资料保留。')
        except Exception as e:
            self.notify(str(e),error=True)

    def rename_environment(self):
        if self.busy:
            return
        name=simpledialog.askstring('重命名环境','仅修改显示名称，不更换容器或登录资料。',initialvalue=self.c.environment_name,parent=self)
        if name is not None:
            try:
                Environments(self.c.root).rename(self.c.environment_id,name)
                self.refresh_environments()
                self.notify('环境已重命名；已有 VS Code 窗口标题在下次连接时更新。')
            except Exception as e:
                self.notify(str(e),error=True)

    def render_projects(self):
        self.refresh_development_selection()
        for widget in self.project_controls:
            self.buttons=[b for b in self.buttons if b is not widget]
            self.inputs=[pair for pair in self.inputs if pair[0] is not widget]
        self.project_controls=[]
        for child in self.project_list.winfo_children():
            child.destroy()
        if not self.project_items:
            wrapped(self.project_list,'目前只有主项目。需要一起开发其他项目时再添加。',size=12).pack(fill='x',pady=8)
        for item in self.project_items:
            box=self.frame(self.project_list,C['field'],corner_radius=10)
            box.pack(fill='x',pady=(0,10))
            head=self.frame(box)
            head.pack(fill='x',padx=12,pady=(12,4))
            name=tk.StringVar(value=item['name'])
            entry=ctk.CTkEntry(head,textvariable=name,height=36,font=(FONT,13),fg_color=C['card'],border_color=C['border'])
            entry.pack(side='left',fill='x',expand=True,padx=(0,10))
            def rename(*_,p=item,v=name):
                p['name']=v.get()
                self.mark_dirty()
            name.trace_add('write',rename)
            readonly=tk.BooleanVar(value=item['readonly'])
            switch=ctk.CTkSwitch(head,text='只读',variable=readonly,width=85,font=(FONT,12),progress_color=C['accent'],
                command=lambda p=item,v=readonly:(p.update(readonly=v.get()),self.mark_dirty()))
            switch.pack(side='left')
            remove=self.button(head,'移除',lambda key=item['id']:self.remove_project(key),width=65)
            remove.pack(side='right',padx=(8,0))
            self.inputs.extend([(entry,'normal'),(switch,'normal')])
            self.project_controls.extend([entry,switch,remove])
            wrapped(box,item['path']+'\n→ /projects/'+item['id'],size=12,color=C['subtle']).pack(fill='x',padx=12,pady=(4,12))

    def add_project(self):
        if self.busy:
            return
        path=filedialog.askdirectory(title='选择附加项目文件夹（可跨磁盘）',parent=self)
        if path:
            if len(self.project_items)>=20:
                self.notify('最多添加 20 个附加项目。',error=True)
                return
            name=Path(path).name or '项目'
            used={p['name'] for p in self.project_items}
            base=name
            i=2
            while name in used:
                name=f'{base} {i}'
                i+=1
            self.project_items.append({'id':'p-'+secrets.token_hex(6),'name':name,'path':path,'readonly':False})
            self.render_projects()
            self.mark_dirty()

    def remove_project(self,key):
        if self.busy:
            return
        if messagebox.askyesno('移除项目挂载','只从本环境配置移除，不删除宿主机文件。\n保存并重启本环境后生效。继续？',parent=self):
            self.project_items=[p for p in self.project_items if p['id']!=key]
            self.render_projects()
            self.mark_dirty()

    def choose_vscode_folder(self):
        selected=self.values['vscode_dir'].get().strip()
        initial=selected if os.path.isdir(selected) else str(self.c.data)
        p=filedialog.askdirectory(title='选择专用的 VS Code 连接配置文件夹',parent=self,initialdir=initial)
        if p:
            self.values['vscode_dir'].set(p)

    def choose_data_folder(self):
        p=filedialog.askdirectory(title='选择独立的空数据文件夹',parent=self)
        if p:
            self.data_target.set(p)

    def migrate_storage(self):
        if self.busy:
            return
        if self.dirty:
            self.notify('请先保存其他修改，再迁移数据目录。',error=True)
            return
        target=self.data_target.get().strip()
        if messagebox.askyesno('迁移工具数据',
            '迁移全部环境到：'+target+'\n\n请先关闭 CCBOX 专用 VS Code 窗口。多个环境时须逐一停止并保留，再迁移。只有一个环境时会自动短暂停止并恢复。\n配置和登录卷保留，原目录留下备份。继续？',parent=self):
            self.task('迁移数据',lambda:self.c.migrate_data(target),self.storage_migrated)

    def storage_migrated(self,result):
        self.storage_current.set(str(self.c.data_root))
        self.data_target.set(str(self.c.data_root))
        self.values['vscode_dir'].set(self.c.config().get('vscode_dir',''))
        self.saved(result)

    def collect_proxy(self):
        d={k:v.get().strip() if k not in ('username','password') else v.get() for k,v in self.proxy_vars.items()}
        if not d['host'] or any(c.isspace() for c in d['host']) or any(c in d['host'] for c in '/@?#'):
            raise FormError('proxy.host','请填写代理服务器主机名或 IP，不包含协议、路径或空格。')
        try:
            port=int(d['port'])
            if not 1<=port<=65535:
                raise ValueError()
        except ValueError:
            raise FormError('proxy.port','端口应为 1–65535 的整数。') from None
        host=f'[{d["host"]}]' if ':' in d['host'] and not d['host'].startswith('[') else d['host']
        try:
            return parse_proxy(f'{d["scheme"]}://{quote(d["username"],safe="")}:{quote(d["password"],safe="")}@{host}:{port}')
        except ValueError as e:
            raise FormError('proxy.host',str(e)) from None

    def collect_config(self):
        c={k:v.get() for k,v in self.values.items()}
        c['projects']=[dict(p) for p in self.project_items]
        for key,lo,hi in [('cpus',1,32),('memory_gb',2,64),('desktop_port',1024,65535)]:
            try:
                n=int(c[key])
                if not lo<=n<=hi:
                    raise ValueError()
            except ValueError:
                raise FormError(key,f'请填写 {lo}–{hi} 的整数。') from None
        if not re.fullmatch(r'(latest|\d+\.\d+\.\d+)',c['claude_version']):
            raise FormError('claude_version','版本请填写 latest 或 x.y.z。')
        try:
            return self.c.validate_config(c)
        except ValueError as e:
            raise FormError(getattr(e,'key','workspace'),str(e)) from None

    def paste_proxy(self):
        try:
            value=self.clipboard_get().strip()
            if '://' not in value:
                value=self.proxy_vars['scheme'].get()+'://'+value
            d=parse_proxy(value)
            if d['scheme']=='socks5':
                d['scheme']='socks5h'
            for k,v in d.items():
                self.proxy_vars[k].set(str(v))
            self.proxy_status.set('已导入连接信息；尚未测试或保存。')
            self.notify('代理已填入。请核对字段，然后测试连接。')
        except Exception:
            self.notify('代理链接格式：socks5h://用户名:密码@服务器:端口',error=True)

    def test_proxy(self):
        if self.busy:
            return
        try:
            proxy=self.collect_proxy()
        except ValueError as e:
            self.form_error(e)
            return
        self.proxy_badge.set('正在测试…')
        self.proxy_status.set('正在通过当前代理查询出口与地理数据库，请稍候。')
        self.task('代理测试',lambda:self.c.test_proxy(proxy),self.proxy_test_done)

    def proxy_test_done(self,result):
        self.proxy_status.set(result)
        self.proxy_badge.set('代理已连通 · 隔离未验证')
        self.proxy_badge_label.configure(text_color=C['accent'])
        self.proxy_time.set('最近检测 '+time.strftime('%H:%M:%S'))
        self.stats['proxy'].set('已连通')
        self.suggested_zone=next((z for z in ZONES if z!='UTC' and z in result),None)
        self.proxy_test_fingerprint=tuple(v.get() for v in self.proxy_vars.values())
        self.zone_button.configure(state='normal' if self.suggested_zone else 'disabled')

    def apply_detected_zone(self):
        if self.suggested_zone:
            self.values['timezone'].set(self.suggested_zone)
            self.notify('已填入建议时区 '+self.suggested_zone+'。保存并重启后生效。')

    def save(self):
        if self.busy:
            return
        try:
            proxy=self.collect_proxy()
            config=self.collect_config()
        except ValueError as e:
            self.form_error(e)
            return
        self.task('保存配置',lambda:self.c.save(config,proxy),self.saved)

    def saved(self,result):
        self.dirty=False
        self.save_status.set('配置已保存 · '+time.strftime('%H:%M'))
        self.saved_label.configure(text_color=C['accent'])
        self.refresh_setup()

    def started(self,result):
        self.status.set(result)
        self.stats['engine'].set('已启动')
        self.stats['checks'].set('待检查')

    def stopped(self,result):
        self.status.set(result)
        self.stats['engine'].set('已停止')
        self.stats['checks'].set('待检查')

    def doctor_done(self,result):
        self.status.set(result)
        self.stats['engine'].set('依赖就绪')

    def runtime_done(self,result):
        self.status.set(result)
        state='已停止' if result=='已停止' else '未创建' if result=='尚未创建环境' else '状态已更新'
        self.stats['engine'].set(state)

    def visit(self):
        url=self.url.get().strip()
        if not url.startswith(('https://','http://','file:///opt/ccbox/')):
            self.form_error(FormError('url','请输入完整的 http(s) 网页链接。'))
            return
        self.task('打开网页',lambda:self.c.open_url(url))

    def outage(self):
        if messagebox.askyesno('模拟代理中断','网关将短暂停止，当前网络会话和桌面连接会中断。测试后重启网关。继续？',parent=self):
            self.task('断网测试',self.c.outage_test,self.show_report)

    def show_report(self,value):
        self.report.configure(state='normal')
        self.report.delete('1.0','end')
        self.report.insert('end',value)
        self.report.configure(state='disabled')
        rows=[]
        for line in value.splitlines():
            m=re.match(r'^\[(PASS|FAIL|MANUAL)\]\s*(.*?)：\s*(.*)$',line)
            if m:
                status,name,detail=m.groups()
                rows.append((name,status,detail))
        if rows:
            self.render_checks(rows)
            fails=sum(r[1]=='FAIL' for r in rows)
            self.stats['checks'].set('需关注' if fails else '结果已出')
            self.check_time.set('最近运行 '+time.strftime('%Y-%m-%d %H:%M:%S')+' · 手动项需另行核对')

    def notify(self,value,error=False):
        self.banner.configure(fg_color='#382626' if error else C['accent_surface'])
        self.banner_label.configure(text=compact_notice(value,error),text_color=C['danger'] if error else C['accent'])
        self.banner.grid()

    def reveal_log(self):
        if not self.log_visible:
            self.toggle_log()
        self.log.focus_set()

    def toggle_log(self):
        self.log_visible=not self.log_visible
        if self.log_visible:
            self.log.grid()
        else:
            self.log.grid_remove()
        self.update_log_title()

    def update_log_title(self):
        self.log_toggle.configure(text=f'操作记录  {self.log_count}  '+('↓' if self.log_visible else '↑'))

    def append_log(self,value):
        self.log.configure(state='normal')
        self.log.insert('end',f'[{time.strftime("%H:%M:%S")}] {value}\n')
        if int(self.log.index('end-1c').split('.')[0])>1200:
            self.log.delete('1.0','201.0')
        self.log.see('end')
        self.log.configure(state='disabled')
        self.log_count+=1
        self.update_log_title()

    def set_busy(self,value):
        self.busy=value
        for b in self.buttons:
            b.configure(state='disabled' if value else 'normal')
        for w,normal in self.inputs:
            w.configure(state='disabled' if value else normal)
        if not value:
            self.zone_button.configure(state='normal' if self.suggested_zone else 'disabled')

    def open_sandbox_editor(self, repair=False):
        from .sandbox_editor import ready
        if self.busy:
            return
        if self.dirty:
            self.notify('请先保存当前修改，再打开沙箱 VS Code。',error=True)
            return
        first=repair or not ready(self.c)
        if first and not messagebox.askyesno('准备沙箱 VS Code',
                '首次准备会下载组件；旧环境升级文字 / 图片剪贴板组件会重启当前沙箱。请先保存容器中的工作。\n\n'
                '项目、账号和代理资料会保留；只处理当前账号环境。\n'
                'VS Code 在容器内运行；为兼容容器，Electron 内层沙箱不启用，Docker 隔离保持开启。\n\n'
                '准备好后继续？',parent=self):
            return
        self.task('打开沙箱 VS Code',lambda:self.c.open_sandbox_vscode(allow_upgrade=first))

    def task(self,title,fn,callback=None):
        if self.busy:
            return
        if self.dirty and title in ('构建镜像','启动环境','打开 CLI','生成 VS Code 配置','连接 VS Code','打开沙箱 VS Code','迁移数据'):
            self.notify('请先保存当前修改，再执行'+title+'。',error=True)
            return
        if title in ('迁移数据','重启环境','构建镜像','打开沙箱 VS Code'):
            self.disconnect_host(quiet=True)
        self.set_busy(True)
        title_label='['+self.c.environment_name+'] '+title
        self.task_status.set(title_label+'进行中…')
        self.task_label.configure(text_color=C['accent'])
        self.banner.grid_remove()
        self.progress.start()
        if title in ('开始开发','打开沙箱 VS Code'):
            self.notify('正在准备当前独立空间，首次可能需要下载。完成后会自动打开编辑器；详细进度可点「查看记录」。')
        def worker():
            try:
                self.messages.put(('done',(title,fn(),callback)))
            except Exception as e:
                self.messages.put(('error',(title,str(e))))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            # A large build log must not starve paint, keyboard or progress events.
            for _ in range(80):
                kind,value=self.messages.get_nowait()
                if kind=='host_approval':
                    self.show_host_approval(value)
                    continue
                if kind=='configuration_saved':
                    self.saved('')
                    continue
                if kind=='log':
                    self.update_development_hint()
                    self.append_log(value)
                    continue
                self.set_busy(False)
                self.progress.stop()
                self.progress.set(0)
                if kind=='error':
                    title,error=value
                    self.task_status.set(title+'未完成')
                    self.task_label.configure(text_color=C['danger'])
                    self.notify(title+'：'+error,error=True)
                    self.append_log('['+self.c.environment_name+'] '+title+'：'+error)
                    if title=='代理测试':
                        self.proxy_badge.set('连接失败')
                        self.proxy_badge_label.configure(text_color=C['danger'])
                        self.proxy_status.set('本次连接失败。请检查地址、认证信息和代理服务状态。')
                        self.proxy_time.set('最近尝试 '+time.strftime('%H:%M:%S'))
                        self.stats['proxy'].set('连接失败')
                        self.suggested_zone=None
                        self.zone_button.configure(state='disabled')
                    if not self.log_visible:
                        self.toggle_log()
                else:
                    title,result,callback=value
                    self.task_status.set(title+'完成 · '+time.strftime('%H:%M:%S'))
                    self.task_label.configure(text_color=C['muted'])
                    self.append_log('['+self.c.environment_name+'] '+result)
                    if callback:
                        callback(result)
                    if title not in ('隔离检查','断网测试','代理测试'):
                        self.notify(result)
                self.refresh_setup()
        except queue.Empty:
            pass
        self._poll_id=self.after(100,self.poll)

    def help(self):
        os.startfile(str(self.c.root/'README.html'))

    def close(self):
        if self.busy:
            messagebox.showinfo('任务进行中','请等待当前任务完成后关闭。',parent=self)
            return
        warning='有未保存修改。关闭后这些修改将丢失。\n\n' if self.dirty else ''
        if messagebox.askokcancel('关闭 CCBOX',warning+'关闭窗口不会停止容器；本机执行连接会断开，并停止本机任务。需要停止容器时，请在首页展开「更多打开方式与停止」。',parent=self):
            self.destroy()

    def destroy(self):
        self.disconnect_host(quiet=True)
        if hasattr(self,'_poll_id'):
            self.after_cancel(self._poll_id)
        super().destroy()


def main():
    mutex=None
    if os.name=='nt':
        import ctypes
        import hashlib
        from .controller import ROOT
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CCBOX.Workbench.2')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
        kernel.CreateMutexW.restype=ctypes.c_void_p
        name='Local\\CCBOX-'+hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]
        mutex=kernel.CreateMutexW(None,False,name)
        if ctypes.get_last_error()==183:
            messagebox.showinfo('CCBOX','此目录的 CCBOX 控制面板已经打开。')
            return
    try:
        App().mainloop()
    except (OSError, RuntimeError, ValueError) as error:
        messagebox.showerror('CCBOX 启动检查', str(error))
