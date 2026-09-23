from pathlib import Path
import runpy

project_root = Path(SPECPATH).resolve()
metadata = runpy.run_path(str(project_root / "app_metadata.py"))

a = Analysis(
    [str(project_root / 'preview.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / 'resources'), 'resources')],
    hiddenimports=['app_metadata'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=metadata['APP_NAME'],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=metadata['APP_NAME'],
)
app = BUNDLE(
    coll,
    name=f"{metadata['APP_NAME']}.app",
    icon=None,
    bundle_identifier=metadata['BUNDLE_IDENTIFIER'],
    version=metadata['APP_VERSION'],
    info_plist={
        'CFBundleShortVersionString': metadata['APP_VERSION'],
        'CFBundleVersion': metadata['APP_VERSION'],
        'CFBundleDocumentTypes': [
            {
                'CFBundleTypeName': 'Markdown document',
                'CFBundleTypeRole': 'Viewer',
                'LSHandlerRank': 'Alternate',
                'CFBundleTypeExtensions': ['md'],
                'LSItemContentTypes': ['net.daringfireball.markdown'],
            },
            {
                'CFBundleTypeName': 'Plain text document',
                'CFBundleTypeRole': 'Viewer',
                'LSHandlerRank': 'Alternate',
                'CFBundleTypeExtensions': ['txt'],
                'LSItemContentTypes': ['public.plain-text'],
            },
        ],
        'UTImportedTypeDeclarations': [
            {
                'UTTypeIdentifier': 'net.daringfireball.markdown',
                'UTTypeDescription': 'Markdown document',
                'UTTypeConformsTo': ['public.plain-text'],
                'UTTypeTagSpecification': {
                    'public.filename-extension': ['md'],
                    'public.mime-type': ['text/markdown'],
                },
            },
        ],
    },
)
