from setuptools import setup
from pathlib import Path
import re
import setup_translate

pkg = 'Extensions.OeMediathek'
version = re.search(r'__version__ = "([^"]+)"',
                    (Path(__file__).parent / 'OeMediathek' / '__init__.py').read_text(encoding='utf-8')).group(1)
setup(name='enigma2-plugin-extensions-OeMediathek',
      version=version,
      description='Enigma2-Plugin zum Streamen der öffentlich-rechtlichen Mediatheken',
      package_dir={pkg: 'OeMediathek'},
      packages=[pkg],
      package_data={pkg: ['logos/*.png', 'logos/defaults/*.png', '*.png', '*.mp4', '*.json', 'meta.xml', 'locale/*/LC_MESSAGES/*.mo']},
      cmdclass=setup_translate.cmdclass,  # for translation
      )
