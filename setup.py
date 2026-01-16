from setuptools import setup


setup(
    name="ha_renderer",
    version="0.0.1",
    packages=["ha_renderer"],
    package_dir={"ha_renderer": "python_render"},
    entry_points={
        "console_scripts": ["ha_renderer=ha_renderer.app:main"],
    },
    package_data={
        "ha_renderer": [
            "templates/*.j2",
            "fonts/*.ttf",
            "fonts/*.otf",
        ]
    },
    include_package_data=True,
)
