import os
import glob
import h5py
import numpy as np
from pyproj import Transformer

os.chdir(os.path.dirname(os.path.abspath(__file__)))

path = sorted(glob.glob("raw_nisar/NISAR_L2_PR_GCOV_*.h5"))[0]
with h5py.File(path, 'r') as f:
    base = "science/LSAR/GCOV/grids/frequencyA"
    x = f[f"{base}/xCoordinates"][:]
    y = f[f"{base}/yCoordinates"][:]
    proj = f[f"{base}/projection"]
    epsg = int(proj[()])
    print("attributs projection :", dict(proj.attrs))

print("epsg lu :", epsg)
print("x :", x.min(), x.max(), "| y :", y.min(), y.max())
print("shape :", len(y), "x", len(x))

to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
for name, px, py in [("bas-gauche", x.min(), y.min()), ("haut-droite", x.max(), y.max()), ("centre", x.mean(), y.mean())]:
    print(name, "->", to_ll.transform(px, py))

to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
print("AOI coin (lon -0.6, lat 43.3) ->", to_utm.transform(-0.6, 43.3))
print("AOI coin (lon 2.2, lat 44.6) ->", to_utm.transform(2.2, 44.6))