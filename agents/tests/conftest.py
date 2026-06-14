import os
import sys

# Чтобы тесты импортировали модули агентов из родительской папки agents/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
