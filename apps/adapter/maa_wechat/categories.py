"""Maa recognition for contact section headers and chevron state."""
import json
import re
import numpy as np
from maa.custom_recognition import CustomRecognition

def label_key(text): return ''.join(re.findall(r'[\u4e00-\u9fff]',text))

def arrow_state(image,y):
    patch=image[y-8:y+8,76:91,:3]
    if patch.shape[:2]!=(16,15): return None
    mask=patch.max(axis=2)<180
    ys,xs=np.where(mask)
    if len(xs)<5: return None
    width=int(xs.max()-xs.min()+1)
    height=int(ys.max()-ys.min()+1)
    if width>=height+2: return 'expanded'
    if height>=width+2: return 'collapsed'
    return None

class ContactCategory(CustomRecognition):
    def analyze(self,context,argv):
        params=argv.custom_recognition_param
        if isinstance(params,str): params=json.loads(params or '{}')
        result=context.run_recognition('ReadContactCategories',argv.image)
        if not result: return None
        labels=[h for h in result.all_results if label_key(h.text)==label_key(params['category'])]
        if len(labels)!=1: return None
        hit=labels[0]
        x,y,w,h=hit.box
        cy=round(y+h/2)
        state=arrow_state(argv.image,cy)
        if state!=params['want']: return None
        counts=[]
        for other in result.all_results:
            ox,oy,ow,oh=other.box
            if ox>430 and abs(oy+oh/2-cy)<12 and other.text.strip().isdigit(): counts.append(int(other.text.strip()))
        if not counts:
            trailing=re.search(r'(\d{1,5})\s*$',hit.text)
            if trailing: counts.append(int(trailing.group(1)))
        return self.AnalyzeResult(box=(76,cy-8,15,16),detail={'category':params['category'],'state':state,'count':counts[0] if len(counts)==1 else None,'header_y':cy})
