from setuptools import setup
import setup_translate

pkg = 'Extensions.OeMediathek'
setup(name='enigma2-plugin-extensions-OeMediathek',
       version='3.0',
       description='Enigma2-Plugin zum Streamen der öffentlich-rechtlichen Mediatheken',
       package_dir={pkg: 'OeMediathek'},
       packages=[pkg],
       package_data={pkg: ['logos/*.png', '*.png', 'locale/*/LC_MESSAGES/*.mo']},
       cmdclass=setup_translate.cmdclass,  # for translation
      )
