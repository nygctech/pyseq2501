#%%
DUMMY = False

from rich import print
from rich.console import Console
from rich.logging import RichHandler

from friendly_hiseq import Components

print("[green]Holding breath...")

if not DUMMY:
    from friendly_hiseq import FriendlyHiSeq
else:
    from dummy import FriendlyHiSeq  # type: ignore

import logging

from ui import init_ui

logging.basicConfig(
    level="NOTSET", format="%(message)s", datefmt="[%X]", handlers=[RichHandler(rich_tracebacks=True)]
)

log = logging.getLogger("rich")
console = Console()

hs = FriendlyHiSeq(console=console, logger=log)
#%%
# init_ui(hs.gen_initialize_seq(skip=[Components.PUMPS, Components.VALVES]))
#%%
hs.initializeCams()

# # %%
hs.image_path = "C:\\Users\\sbsuser\\Desktop\\goff-rotation\\images\\"
x_begin = 20
y_begin = 15
size = 1
pos = hs.position("A", [x_begin, y_begin, x_begin - size, y_begin - size])
# hs.y.move(pos["y_initial"])
#%%
hs.x.move(pos["x_initial"])
hs.z.move([20000, 20000, 20000])
hs.obj.move(30000)
hs.optics.move_ex("green", "open")
hs.optics.move_ex("red", "open")
hs.optics.move_em_in(True)
#%%
hs.y.move(pos["y_initial"], precision=10)
#%%
hs.take_picture(64, "128")
# # %%
# hs.initializeCams()
# # %%

# %%
