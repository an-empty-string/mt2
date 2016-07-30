from mt2 import *
from IPython.terminal.embed import InteractiveShellEmbed

with midi_pair(mido.open_input("MT2 Input", virtual=True), mido.open_output("MT2 Output", virtual=True)):
    InteractiveShellEmbed(local_ns=locals())()
