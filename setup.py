from setuptools import setup


setup(name='StrainFacts',
      version='0.1',
      summary='TODO',
      url='http://github.com/bsmith89/StrainFacts',
      author='Byron J. Smith',
      author_email='me@byronjsmith.com',
      packages=['sfacts'],
      install_requires=[],
      dependency_links=[],
      entry_points={
        'console_scripts': [
          'sfacts = sfacts.__main__:main'
        ]
      },
      zip_safe=False
)