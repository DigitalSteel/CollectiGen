# Explicit local qualification only; no native launch/signing authority.
import sys
sys.path.insert(0,SPECPATH)
from qt_qualification_recipe import build_qt_recipe
bundle=build_qt_recipe('reader',globals())
