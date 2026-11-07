"""Read-only guard on Maa-captured pixels, not legacy screen coordinates."""
import numpy as np


def check_frame(image):
    if image.shape[:2] != (1040,1920): raise ValueError('FRAME_GEOMETRY_CHANGED')
    xs=np.arange(240,641)
    ys=np.arange(140,1000,17)
    # Test the actual adjacent-pixel edge. A wide ±4 probe is biased when
    # WeChat's scrollbar covers x470..477, despite the divider staying x480.
    left=image[ys[:,None],(xs-1)[None,:],:3].astype(np.int16)
    right=image[ys[:,None],xs[None,:],:3].astype(np.int16)
    lmin,lmax=left.min(axis=2),left.max(axis=2)
    rmin,rmax=right.min(axis=2),right.max(axis=2)
    hits=(lmax-lmin<=3)&(rmax-rmin<=3)&(lmin>=220)&(lmin<=240)&(rmin>=242)&(rmin<=252)&(rmin-lmax>=7)
    candidates=xs[hits.sum(axis=0)>=36].tolist()
    if len(candidates)!=1: raise ValueError('DIVIDER_NOT_VERIFIABLE')
    detected=candidates[0]
    if abs(detected-480)>1: raise ValueError(f'DIVIDER_CHANGED:{detected}')
    return detected


def navigation_entry_safe(image, hits):
    """Only recover from the observed Discover pane, never relax a send guard."""
    if image.shape != (1040,1920,3): return False
    labels=[]
    for hit in hits:
        b=hit.get('box',[])
        if isinstance(b,dict): b=[b[k] for k in ('x','y','w','h')]
        if len(b)==4 and 65<=b[0]<400 and 35<=b[1]<75: labels.append(str(hit.get('text','')).strip())
    if labels != ['发现']: return False
    sidebar=image[320:900,5:50,:3].astype(np.int16)
    background=np.median(sidebar.reshape(-1,3),axis=0)
    return bool(background.min()>=210 and background.max()<=235 and background.max()-background.min()<=3)
