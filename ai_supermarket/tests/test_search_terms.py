"""本项目原创。短查询可发现长标签，同时保留资格过滤和精确匹配优先。"""
import unittest
from ai_supermarket.catalog import Catalog
from ai_supermarket.common import MarketError


class SearchTermsTests(unittest.TestCase):
    def catalog(self, *items):
        catalog = Catalog.__new__(Catalog)
        catalog.items = {item['id']: item for item in items}
        return catalog

    def item(self, id='docs', **updates):
        return {'id': id, 'version': '1.0.0', 'title': '开源技术文档技能',
                'keywords': ['技术文档开源技能', 'documentation writer'],
                'state': 'active', 'allowed_uses': ['project_local'], 'kind': 'skill', **updates}

    def test_short_chinese_and_english_terms_find_longer_labels(self):
        catalog = self.catalog(self.item())
        for query in ('文档', '文档 接入说明', 'documentation', 'WRITER'):
            with self.subTest(query=query):
                self.assertEqual(['docs'], [r['id'] for r in catalog.search(query)])
        for query in ('文', 'doc', 'writerly', '不存在的火星任务'):
            with self.subTest(query=query):
                self.assertEqual([], catalog.search(query))

    def test_exact_keyword_outranks_partial_and_limit_stays_bounded(self):
        catalog = self.catalog(self.item('a-partial'), self.item('z-exact', keywords=['文档']), self.item('b-partial'))
        self.assertEqual(['z-exact'], [r['id'] for r in catalog.search('文档', limit=1)])

    def test_partial_match_never_bypasses_kind_state_or_use(self):
        catalog = self.catalog(self.item('hidden', state='suspended'),
                               self.item('wrong-use', allowed_uses=['commercial_use']),
                               self.item('wrong-kind', kind='template'), self.item('eligible'))
        self.assertEqual(['eligible'], [r['id'] for r in catalog.search('文档', kind='skill')])

    def test_english_phrase_does_not_match_inside_longer_words(self):
        catalog = self.catalog(self.item(keywords=['user research'], title='访谈方法'))
        for query in ('enduser researcher', 'end-user researcher', 'enduser research_team'):
            with self.subTest(query=query):
                self.assertEqual([], catalog.search(query))

    def test_partial_phrase_cannot_outrank_a_complete_topic(self):
        catalog = self.catalog(
            self.item('a-wrong', keywords=['user research'], title='访谈方法'),
            self.item('z-relevant', keywords=['researchers'], title='研究者名录'))
        self.assertEqual(['z-relevant'], [r['id'] for r in catalog.search('user researchers', limit=1)])

    def test_phrase_accepts_punctuation_case_and_whitespace(self):
        catalog = self.catalog(self.item(keywords=['user research'], title='访谈方法'),
                               self.item('a-partial', keywords=['user'], title='普通用户'))
        for query in ('(USER RESEARCH)', 'user  research'):
            with self.subTest(query=query):
                self.assertEqual(['docs'], [r['id'] for r in catalog.search(query, limit=1)])
        with self.assertRaises(MarketError) as error:
            catalog.search('user\tresearch')
        self.assertEqual('invalid_params', error.exception.code)

    def test_fullwidth_query_and_seller_labels_match_ascii_equivalents(self):
        catalog = self.catalog(self.item('csv-check', title='数据表核验', keywords=['csv']),
                               self.item('json-check', title='ＪＳＯＮ检查', keywords=['ＪＳＯＮ']))
        for query in ('CSV', 'ＣＳＶ', '请检查ＣＳＶ文件'):
            with self.subTest(query=query):
                self.assertEqual(['csv-check'], [r['id'] for r in catalog.search(query)])
        for query in ('json', 'ＪＳＯＮ'):
            with self.subTest(query=query):
                self.assertEqual(['json-check'], [r['id'] for r in catalog.search(query)])

    def test_unicode_spaces_match_phrases_without_relaxing_word_boundaries(self):
        catalog = self.catalog(self.item(keywords=['user research'], title='访谈方法'))
        for query in ('ＵＳＥＲ　ＲＥＳＥＡＲＣＨ', 'user\u00a0research'):
            with self.subTest(query=query):
                self.assertEqual(['docs'], [r['id'] for r in catalog.search(query)])
        for query in ('ｅｎｄｕｓｅｒ　ｒｅｓｅａｒｃｈｅｒ', 'ｕｓｅｒ＿ｒｅｓｅａｒｃｈ'):
            with self.subTest(query=query):
                self.assertEqual([], catalog.search(query))

    def test_normalized_short_labels_keep_eligibility_and_original_metadata(self):
        original = self.item('csv-check', title='ＣＳＶ检查', keywords=['其他标签'])
        catalog = self.catalog(original, self.item('hidden', title='CSV', state='suspended'),
                               self.item('wrong-use', title='CSV', allowed_uses=['commercial_use']),
                               self.item('wrong-kind', title='CSV', kind='template'))
        found = catalog.search('csv', kind='skill')
        self.assertEqual(['csv-check'], [r['id'] for r in found])
        self.assertEqual('ＣＳＶ检查', found[0]['title'])
        self.assertIs(original, found[0])


if __name__ == '__main__':
    unittest.main()
