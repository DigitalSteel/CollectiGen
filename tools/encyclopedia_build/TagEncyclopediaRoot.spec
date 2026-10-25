# Stock onedir recipe for pinned fixture or normal source inputs; no native execution authority.
import sys
sys.path.insert(0, SPECPATH)
from recipe_support import build_recipe
bundle = build_recipe('root', globals())
