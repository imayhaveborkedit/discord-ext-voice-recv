# -*- coding: utf-8 -*-

from setuptools import setup
import re

with open('discord/ext/voice_recv/__init__.py') as f:
    version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]', f.read(), re.MULTILINE).group(1)  # type: ignore

if not version:
    raise RuntimeError('version is not set')

if version.endswith(('a', 'b', 'rc')):
    # append version identifier based on commit count
    try:
        import subprocess

        p = subprocess.Popen(['git', 'rev-list', '--count', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if out:
            version = version + out.decode('utf-8').strip()
    except Exception:
        pass

with open('README.md') as f:
    readme = f.read()

extras_require = {
    'extras': [
        'SpeechRecognition',
    ]
}

setup(
    name='discord-ext-voice_recv',
    author='Imayhaveborkedit',
    url='https://github.com/imayhaveborkedit/discord-ext-voice-recv',
    version=version,
    packages=['discord.ext.voice_recv', 'discord.ext.voice_recv.extras'],
    license='MIT',
    description='Experimental voice receive extension for discord.py',
    long_description=readme,
    long_description_content_type='text/markdown',
    include_package_data=True,
    python_requires='>=3.8',
    install_requires=['discord.py[voice]>=2.5'],
    extras_require=extras_require,
    zip_safe=False,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'License :: OSI Approved :: MIT License',
        'Intended Audience :: Developers',
        'Natural Language :: English',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Operating System :: POSIX',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: MacOS',
        'Topic :: Multimedia :: Sound/Audio :: Capture/Recording',
    ],
)
