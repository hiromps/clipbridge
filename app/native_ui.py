"""Small, keyboard-accessible canvas controls matching ClipBridge's web palette."""
import tkinter as tk

CANVAS = "#eceef1"
SURFACE = "#f3f4f6"
INK = "#1c2b3a"
MUTED = "#718190"
MARKER = "#c0e79e"


def rounded(canvas, x1, y1, x2, y2, radius, **options):
    radius = min(radius, (x2-x1)/2, (y2-y1)/2)
    return canvas.create_polygon(
        x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
        x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
        x1,y2, x1,y2-radius, x1,y1+radius, x1,y1,
        smooth=True, splinesteps=28, **options)


class Pill(tk.Canvas):
    def __init__(self, master, text, command, primary=False, selected=False, compact=False):
        super().__init__(master, background=SURFACE, highlightthickness=0,
                         borderwidth=0, width=120, height=48 if compact else 56, takefocus=1,
                         cursor="hand2")
        self.caption, self.command = text, command
        self.primary, self.selected = primary, selected
        self.hover = self.focused = False
        self.bind("<Configure>", lambda _: self.draw())
        self.bind("<Enter>", lambda _: self.set_hover(True))
        self.bind("<Leave>", lambda _: self.set_hover(False))
        self.bind("<FocusIn>", lambda _: self.set_focus(True))
        self.bind("<FocusOut>", lambda _: self.set_focus(False))
        self.bind("<ButtonRelease-1>", lambda _: self.activate())
        self.bind("<Return>", lambda _: self.activate())
        self.bind("<space>", lambda _: self.activate())

    def activate(self):
        self.focus_set()
        self.command()
        return "break"

    def set_hover(self, value):
        self.hover = value
        self.draw()

    def set_focus(self, value):
        self.focused = value
        self.draw()

    def draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 5:
            return
        if self.selected:
            fill, color = MARKER, INK
        elif self.primary:
            fill, color = ("#304453" if self.hover else INK), "#ffffff"
        else:
            fill, color = ("#fafdff" if self.hover else "#ffffff"), INK
        if not self.primary and not self.selected:
            self.capsule(3, 7, width-3, height-1, "#e2e6ec")
            self.capsule(3, 5, width-3, height-3, "#e8ebf0")
        if self.focused:
            self.capsule(0, 0, width, height-4, "#007be5")
        self.capsule(2, 2, width-2, height-6, fill)
        self.create_text(width/2, (height-4)/2, text=self.caption, fill=color,
                         font=("Yu Gothic UI", 10, "bold"), width=max(60,width-28), justify="center")

    def capsule(self, x1, y1, x2, y2, color):
        diameter = min(y2-y1, x2-x1)
        self.create_rectangle(x1+diameter/2, y1, x2-diameter/2, y2, fill=color, outline="")
        self.create_oval(x1, y1, x1+diameter, y2, fill=color, outline="")
        self.create_oval(x2-diameter, y1, x2, y2, fill=color, outline="")
