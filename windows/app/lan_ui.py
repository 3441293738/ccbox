"""Beginner-friendly, explicit desktop sharing controls."""
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from .lan import local_addresses, apply_access, sharing_details
from .ui_components import COLORS as C, FONT, text, wrapped


class LanUI:
    def open_lan_settings(self):
        self.show('环境配置')
        self.toggle_section('lan', True)
        self.ensure_visible(self.fields['lan_address']['widget'])

    def lan_settings(self, parent):
        f = self.disclosure(parent, 'lan', '局域网访问', '让同一可信网络的其他电脑访问当前沙箱。默认关闭，每个账号空间单独设置。')
        switch = ctk.CTkSwitch(f, text='允许局域网访问当前空间', variable=self.values['lan_enabled'],
                              font=(FONT, 13), progress_color=C['accent'])
        switch.pack(anchor='w', pady=(8, 16))
        self.inputs.append((switch, 'normal'))
        self.field(f, 'lan_address', '本机局域网 IPv4 地址', self.values['lan_address'],
                   hint='点击检测自动填写，也可手动选择本机网卡地址。只绑定这个地址，保留 127.0.0.1 本机入口。')
        row = self.action_row(f)
        self.add_action(row, '检测本机地址', self.detect_lan, width=130)
        self.add_action(row, '保存并应用访问设置', self.apply_lan, True, width=170)
        row = self.action_row(f)
        self.add_action(row, '查看连接信息', self.show_lan_connection, width=130)
        self.add_action(row, '重置访问口令', lambda: self.apply_lan(rotate=True), width=130)
        wrapped(f, '访问口令由程序随机生成，与 Claude 密码无关。持有口令的人可操作整个桌面、共享项目及当前账号。HTTP 不加密，请仅在可信局域网使用；不要设置公网端口转发。', size=12, color=C['warning']).pack(fill='x', pady=(14, 0))
        wrapped(f, '应用时只重连桌面网关，不停止编辑器和登录卷。防火墙仅放行当前端口、专用网络和本地子网；工具不会自动关闭或修改系统防火墙。', size=12).pack(fill='x', pady=(8, 0))

    def detect_lan(self):
        found = []
        def scan():
            found.extend(local_addresses())
            return '本机网卡检测完成。' if found else '未找到私有 IPv4 地址，请检查网络或手动填写。'
        def done(_):
            if found:
                self.values['lan_address'].set(found[0])
                self.notify('已填写 '+found[0]+'；多个地址：'+'、'.join(found)+'。请核对使用的网卡，再应用。')
        self.task('检测局域网地址', scan, done)

    def apply_lan(self, rotate=False):
        if self.busy:
            return
        try:
            config = self.collect_config()
            stored = self.c.config()
            keys = set(config) - {'lan_enabled', 'lan_address'}
            if any(config.get(k) != stored.get(k) for k in keys) or self.collect_proxy() != self.c.credentials().get('proxy'):
                raise ValueError('请先保存其他项目、代理或端口修改，再单独应用局域网访问设置。')
        except ValueError as e:
            self.notify(str(e), error=True)
            return
        prompt = ('重置后，已连接设备会断开，旧口令立即失效。' if rotate else
                  '开启后，持有口令的局域网设备可控制当前沙箱。HTTP 为明文连接，仅用于可信网络。' if config['lan_enabled'] else
                  '关闭后，局域网设备将断开；本机仍可使用。')
        if not messagebox.askyesno('应用桌面访问设置', prompt+'\n\n只重连当前桌面网关，项目、编辑器与登录资料保留。继续？', parent=self):
            return
        controller = self.c
        def work():
            controller.save(config, controller.credentials()['proxy'])
            self.messages.put(('configuration_saved', None))
            return apply_access(controller, rotate=rotate)
        self.task('应用访问设置', work)

    def show_lan_connection(self):
        result = []
        controller = self.c
        def read():
            result.extend(sharing_details(controller))
            return '连接信息已读取。访问口令只在连接弹窗显示，不写入操作记录。'
        def show(_):
            url, token = result
            dialog = ctk.CTkToplevel(self)
            dialog.title('当前生效的局域网连接')
            dialog.geometry('640x425')
            dialog.transient(self)
            dialog.grab_set()
            box = ctk.CTkFrame(dialog, fg_color=C['bg'])
            box.pack(fill='both', expand=True)
            inner = ctk.CTkFrame(box, fg_color='transparent')
            inner.pack(fill='both', expand=True, padx=26, pady=22)
            text(inner, '在另一台电脑的浏览器中打开', size=19, weight='bold').pack(anchor='w')
            wrapped(inner, '当前空间：'+controller.environment_name, size=12).pack(fill='x', pady=(6, 12))
            address = ctk.CTkEntry(inner, height=38, font=(FONT, 13))
            address.pack(fill='x'); address.insert(0, url); address.configure(state='readonly')
            text(inner, '访问口令（不是 Claude 密码）', size=13).pack(anchor='w', pady=(15, 5))
            password = ctk.CTkEntry(inner, height=38, show='●', font=(FONT, 13))
            password.pack(fill='x'); password.insert(0, token); password.configure(state='readonly')
            status = tk.StringVar(value='地址和口令请分开发送给可信使用者。')
            def copy(value, label):
                self.clipboard_clear(); self.clipboard_append(value)
                status.set(label+'已复制，请在另一台设备粘贴；使用后可清空剪贴板。')
            row = self.action_row(inner)
            for title, action in [('复制地址', lambda: copy(url, '地址')), ('复制口令', lambda: copy(token, '口令'))]:
                self.button(row, title, action, track=False, width=130).pack(side='left', padx=(0, 10))
            wrapped(inner, '', textvariable=status, size=12).pack(fill='x', pady=(12, 0))
            wrapped(inner, '同一桌面会被共享，不是每位访问者独立登录。打不开时检查主机防火墙、访客 Wi-Fi 隔离和网卡 IP 是否变化。', size=12, color=C['warning']).pack(fill='x', pady=(10, 0))
            self.button(inner, '关闭', dialog.destroy, track=False, width=100).pack(anchor='e', pady=(12, 0))
        self.task('读取连接信息', read, show)
