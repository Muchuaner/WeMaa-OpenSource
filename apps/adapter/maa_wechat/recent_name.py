"""Crop a visually verified UI badge out of a recent row's name, never text."""
def name_roi(y, badges):
    boxes=[]
    for hit in badges:
        b=hit.get('box',())
        if isinstance(b,dict):b=[b[k] for k in ('x','y','w','h')]
        if len(b)!=4:continue
        x,hy,w,h=b
        if 125<=x<397 and 5<=w<=25 and 5<=h<=25 and y-25<=hy+h/2<=y+4:
            boxes.append(x)
    return [115,y-25,min(boxes)-118,29] if len(boxes)==1 else None

def row_name(runtime,image,y):
    badges=runtime.recognize_image('ChatTitleBlueBadge',image,roi=[115,y-25,285,29])
    roi=name_roi(y,badges)
    hits=runtime.recognize_image('ReadRecentList',image,roi=roi)
    def box(hit):
        b=hit['box'];return [b[k] for k in ('x','y','w','h')] if isinstance(b,dict) else b
    hits=[h for h in hits if y-25<=box(h)[1]+box(h)[3]/2<=y+4]
    return ' '.join(h['text'] for h in sorted(hits,key=lambda h:box(h)[0])).strip(),hits
