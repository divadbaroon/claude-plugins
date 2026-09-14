import io
import unittest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject, ArrayObject, NumberObject
from human_compact.trajectory.project_paper_links import extract

class PaperLinksTests(unittest.TestCase):
    def test_platforms_dedup_and_punctuation(self):
        result=extract(b'https://github.com/team/repo. https://github.com/team/repo https://huggingface.co/datasets/team/data https://osf.io/abcd/ https://example.org/no', 'paper.txt')
        self.assertEqual([x['platform'] for x in result['links']],['GitHub','Hugging Face','OSF'])
        self.assertEqual(result['links'][0]['url'],'https://github.com/team/repo')
    def test_pdf_hyperlink_without_visible_url(self):
        writer=PdfWriter();page=writer.add_blank_page(width=100,height=100)
        page[NameObject('/Annots')]=ArrayObject([DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Link'),NameObject('/Rect'):ArrayObject([NumberObject(0)]*4),NameObject('/A'):DictionaryObject({NameObject('/S'):NameObject('/URI'),NameObject('/URI'):TextStringObject('https://osf.io/abcd/')})})])
        output=io.BytesIO();writer.write(output)
        self.assertEqual(extract(output.getvalue(),'paper.pdf')['links'][0]['url'],'https://osf.io/abcd')
    def test_bad_document(self):
        with self.assertRaises(Exception):extract(b'not a PDF','paper.pdf')
