import unittest
import numpy as np
from contact_alignment import overlap,photo_match,normalized_photo,stationary

def row(name,seed):
    return {'name':name,'photo':np.random.default_rng(seed).integers(0,256,(24,24,3),dtype=np.uint8)}

class AlignmentTests(unittest.TestCase):
    def test_ordered_overlap(self):
        a,b,c,d,e=[row(str(i),i) for i in range(5)]
        self.assertEqual(overlap([a,b,c,d],[c,d,e]),2)
    def test_gap_rejected(self):
        with self.assertRaises(ValueError): overlap([row('a',1),row('b',2)],[row('c',3),row('d',4)])
    def test_ambiguous_duplicates_rejected(self):
        a=row('same',1)
        with self.assertRaises(ValueError): overlap([a,a,a],[a,a,a])
    def test_repeated_company_photo_uses_names(self):
        a,b,c,d=[row(name,1) for name in 'abcd']
        self.assertEqual(overlap([a,b,c],[b,c,d]),2)
    def test_first_page(self): self.assertEqual(overlap([],[row('a',1)]),0)
    def test_small_render_noise(self):
        a=row('a',1)['photo']
        b=np.clip(a.astype(np.int16)+2,0,255).astype(np.uint8)
        self.assertTrue(photo_match(a,b))
    def test_transparent_photo_selected_background(self):
        a=np.full((24,24,3),237,dtype=np.uint8)
        b=np.full((24,24,3),[100,180,20],dtype=np.uint8)
        a[5:19,5:19]=[0,0,255]
        b[5:19,5:19]=[0,0,255]
        self.assertTrue(photo_match(normalized_photo(a,[237,237,237]),normalized_photo(b,[100,180,20])))
    def test_one_changed_photo_requires_named_neighbor_context(self):
        a,b,c,d,e=[row(str(i),i) for i in range(5)]
        changed=row(c['name'],99)
        self.assertEqual(overlap([a,b,c,d],[b,changed,d,e]),3)
        with self.assertRaises(ValueError): overlap([a,b,c,d],[b,row('other',99),d,e])
    def test_stationary_coordinates_and_names(self):
        a,b,c,d=[dict(row(str(i),i),y=100+i*54) for i in range(4)]
        changed=dict(row(c['name'],99),y=c['y'])
        self.assertTrue(stationary([a,b,c,d],[a,b,changed,d]))
        shifted=[dict(r,y=r['y']-54) for r in [a,b,c,d]]
        self.assertFalse(stationary([a,b,c,d],shifted))
    def test_clipped_edge_requires_three_photo_anchors_and_exact_unique_name(self):
        a,b,c,d,e=[row(str(i),i) for i in range(5)]
        self.assertEqual(overlap([a,b,c,d],[a,b,c,row(d['name'],99),e]),4)
        with self.assertRaises(ValueError): overlap([a,b,c,d],[a,b,c,row('other',99),e])
        with self.assertRaises(ValueError): overlap([b,c,d],[b,c,row(d['name'],99),e])
    def test_two_render_changes_require_long_unique_ordered_context(self):
        original=[row(str(i),i) for i in range(9)]
        changed=list(original)
        changed[2]=row('2',98)
        changed[8]=row('8',99)
        self.assertEqual(overlap(original,changed),9)
        changed[8]=row('other',99)
        with self.assertRaises(ValueError): overlap(original,changed)
    def test_symbol_only_name_preserved_as_unique_context(self):
        original=[row(name,i) for i,name in enumerate(['z','#','&','a','b','c','d','e'])]
        changed=list(original)
        changed[1]=row('#',99)
        self.assertEqual(overlap(original,changed),8)
    def test_two_interior_changes_allow_one_ocr_character_with_long_context(self):
        original=[row('name'+str(i),i) for i in range(9)]
        changed=list(original)
        changed[4]=row('nane4',98)
        changed[5]=row('name5',99)
        self.assertEqual(overlap(original,changed),9)
        changed[5]=row('other',99)
        with self.assertRaises(ValueError): overlap(original,changed)
