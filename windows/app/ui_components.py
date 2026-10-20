"""CCBOX's desktop design tokens and reusable, keyboard-operable controls."""
import tkinter as tk
import customtkinter as ctk

COLORS = {
    'bg': '#111617', 'sidebar': '#0c1011', 'card': '#1a2123', 'raised': '#222b2e',
    'field': '#101719', 'border': '#46565b', 'divider': '#303c3f',
    'text': '#edf5f2', 'muted': '#adbbb8', 'subtle': '#97a9a4',
    'accent': '#b2f1c9', 'accent_hover': '#caf8da', 'on_accent': '#13251b',
    'accent_surface': '#21352b', 'hero': '#182923', 'hero_border': '#446954',
    'blue': '#a7cafa', 'warning': '#f4d28e', 'danger': '#ffb4ad',
}
FONT = 'Microsoft YaHei UI'
MONO = 'Cascadia Code'


class ActionButton(ctk.CTkButton):
    """CTk visuals with an explicit Tab stop, Enter/Space and visible focus ring."""
    def __init__(self, master, text, command, kind='secondary', **kwargs):
        self.kind = kind
        color = COLORS
        primary = kind == 'primary'
        self.base_border = color['accent'] if primary else color['border']
        super().__init__(master, text=text, command=command,
                         fg_color=color['accent'] if primary else color['raised'],
                         hover_color=color['accent_hover'] if primary else '#303e40',
                         text_color=color['on_accent'] if primary else color['text'],
                         text_color_disabled='#81938c', border_width=2,
                         border_color=self.base_border, corner_radius=9, height=42,
                         font=(FONT, 13, 'bold' if primary else 'normal'), **kwargs)
        tk.Frame.configure(self, takefocus=1)
        tk.Frame.bind(self, '<FocusIn>', lambda _: self.configure(border_color=color['blue']), add='+')
        tk.Frame.bind(self, '<FocusOut>', lambda _: self.configure(border_color=self.base_border), add='+')
        tk.Frame.bind(self, '<Return>', self._keyboard_click, add='+')
        tk.Frame.bind(self, '<space>', self._keyboard_click, add='+')

    def _keyboard_click(self, _):
        self.invoke()
        return 'break'


def text(master, value='', size=14, color=None, weight='normal', **kwargs):
    return ctk.CTkLabel(master, text=value, text_color=color or COLORS['text'],
                        font=(FONT, size, weight), anchor='w', justify='left', **kwargs)


def wrapped(master, value='', size=13, color=None, **kwargs):
    label = text(master, value, size=size, color=color or COLORS['muted'], wraplength=600, **kwargs)
    # Binding on the frame (rather than CTk's internal canvas) preserves draw handlers.
    def resize(event):
        if not label.winfo_exists() or not label._label.winfo_exists():
            return
        scaling = label._get_widget_scaling()
        label.configure(wraplength=max(150, int(event.width / scaling) - 12))
    binding = tk.Misc.bind(master, '<Configure>', resize, add='+')
    def cleanup(event):
        if event.widget is label and master.winfo_exists():
            tk.Misc.unbind(master, '<Configure>', binding)
    tk.Misc.bind(label, '<Destroy>', cleanup, add='+')
    return label


class CubeMark(tk.Canvas):
    def __init__(self, master, size=36, bg=None):
        super().__init__(master, width=size, height=size, highlightthickness=0,
                         bg=bg or COLORS['sidebar'])
        self.bind('<Configure>', self.draw)

    def draw(self, event):
        self.delete('all')
        s = min(event.width, event.height)
        xy = [(s*.5,s*.1),(s*.85,s*.28),(s*.85,s*.71),(s*.5,s*.9),(s*.15,s*.71),(s*.15,s*.28)]
        self.create_polygon(*sum(([x,y] for x,y in xy), []), outline=COLORS['accent'], fill='', width=2)
        self.create_line(s*.15,s*.28,s*.5,s*.47,s*.85,s*.28,fill=COLORS['accent'],width=2)
        self.create_line(s*.5,s*.47,s*.5,s*.9,fill=COLORS['accent'],width=2)


class RouteDiagram(tk.Canvas):
    """A code-rendered architecture diagram, not a live connection indicator."""
    def __init__(self, master):
        super().__init__(master, width=224, height=148, bg=COLORS['hero'], highlightthickness=0)
        self.bind('<Configure>', self.draw)

    def draw(self, event):
        self.delete('all')
        w, h = event.width, event.height
        scale = min(w / 224, h / 148)
        ox, oy = (w - 224*scale)/2, (h-148*scale)/2
        def xy(*values):
            return [v*scale + (ox if i%2==0 else oy) for i,v in enumerate(values)]
        for y in range(8,145,14):
            for x in range(5,224,14):
                self.create_oval(*xy(x,y,x+1,y+1),fill='#355044',outline='')
        for x, caption, sub in [(5,'CLI','工作进程'),(81,'UDS','隔离通道'),(157,'PROXY','代理出口')]:
            self.create_rectangle(*xy(x,40,x+61,103), fill='#1b3026',outline='#537c64',width=1)
            self.create_text(*xy(x+30,61),text=caption,fill=COLORS['accent'],font=(MONO,max(9,int(10*scale)),'bold'))
            self.create_text(*xy(x+30,84),text=sub,fill=COLORS['muted'],font=(FONT,max(8,int(8*scale))))
        for x in (66,142):
            self.create_line(*xy(x,70,x+14,70),fill=COLORS['accent'],arrow='last')
        self.create_text(*xy(112,128),text='设计拓扑 · 非实时状态',fill=COLORS['muted'],font=(FONT,max(8,int(8*scale))))
