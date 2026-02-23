import sys, traceback
sys.path.insert(0, r'C:\Dev\rtspwebrtc\cpp\build\Release')
print('python version:', sys.version)
try:
    import rtspwebrtc_cpp as mod
    print('import OK')
    names = [n for n in dir(mod) if not n.startswith('_')]
    print('exported names (first 50):', names[:50])
except Exception:
    traceback.print_exc()
    raise
