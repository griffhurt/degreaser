import os

# Run Qt headless unless a platform was chosen explicitly.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
