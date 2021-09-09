# -*- coding: utf-8 -*-

from setuptools import setup
import re

with open('discord/ext/voice_recv/__init__.py') as f:
    version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]', f.read(), re.MULTILINE).group(1)

if not version:
    raise RuntimeError('version is not set')

if version.endswith(('a', 'b', 'rc')):
    # append version identifier based on commit count
    try:
        import subprocess
        p = subprocess.Popen(['git', 'rev-list', '--count', 'HEAD'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if out:
            version = version + out.decode('utf-8').strip()
    except Exception:
        pass

with open('README.md') as f:
    readme = f.read()

setup(name='discord-ext-voice_recv',
      author='Imayhaveborkedit',
      url='https://github.com/imayhaveborkedit/discord.py/tree/voice-recv-ext',
      version=version,
      packages=['discord.ext.voice_recv'],
      license='MIT',
      description='Experimental voice receive extension for discord.py',
      long_description=readme,
      include_package_data=True,
      python_requires='>=3.7',
      install_requires=['discord.py[voice]>=1.7.3'],
      extras_require=None,
      zip_safe=False,
      classifiers=[
        'Development Status :: 3 - Alpha',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
        'Intended Audience :: Developers',
        'Natural Language :: English',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        "Operating System :: POSIX",
        "Operating System :: Windows",
        "Operating System :: MacOS",
      ]
)
