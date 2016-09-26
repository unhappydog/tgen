#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import codecs
import json
import re
from argparse import ArgumentParser
from collections import deque
from itertools import islice
from recordclass import recordclass

from ufal.morphodita import Tagger, Forms, TaggedLemma, TaggedLemmas, TokenRanges, Analyses, Indices

from tgen.logf import log_info

# Start IPdb on error in interactive mode
from tgen.debug import exc_info_hook
import sys
sys.excepthook = exc_info_hook


#
# Representation of DAIs & Delex. instructions (TODO make these unified accross datasets)


class DAI(recordclass('DAI', ['type', 'slot', 'value'])):
    """Simple representation of a single dialogue act item."""

    def __unicode__(self):
        if self.slot is None:
            return self.type + '()'
        if self.value is None:
            return self.type + '(' + self.slot + ')'
        quote = '\'' if (' ' in self.value or ':' in self.value) else ''
        return self.type + '(' + self.slot + '=' + quote + self.value + quote + ')'


class Abst(recordclass('Abst', ['slot', 'value', 'start', 'end'])):
    """Simple representation of a single abstraction instruction."""

    def __unicode__(self):
        quote = '"' if ' ' in self.value or ':' in self.value else ''
        return (self.slot + '=' + quote + self.value + quote + ':' +
                str(self.start) + '-' + str(self.end))


class DA(object):
    """Dialogue act – basically a list of DAIs."""

    def __init__(self):
        self.dais = []

    def __getitem__(self, idx):
        return self.dais[idx]

    def __setitem__(self, idx, value):
        self.dais[idx] = value

    def append(self, value):
        self.dais.append(value)

    def __unicode__(self):
        return '&'.join([unicode(dai) for dai in self.dais])

    def __str__(self):
        return unicode(self).encode('ascii', errors='xmlcharrefreplace')

    def __len__(self):
        return len(self.dais)

    @staticmethod
    def parse(da_text):
        """Parse a DA string into DAIs (DA types, slots, and values)."""
        da = DA()

        # split acc. to DAIs, trim final bracket
        for dai_text in da_text[:-1].split(')&'):
            da_type, svp = dai_text.split('(', 1)

            if not svp:  # no slot + value (e.g. 'hello()')
                da.append(DAI(da_type, None, None))
                continue

            if '=' not in svp:  # no value (e.g. 'request(to_stop)')
                da.append(DAI(da_type, svp, None))
                continue

            slot, value = svp.split('=', 1)
            if value.startswith('"'):  # remove quotes
                value = value[1:-1]

            da.append(DAI(da_type, slot, value))

        return da

    def has_value(self, value):
        """If the DA contains the given value, return the corresponding slot; return None
        otherwise. Abstracts away from "and" and "or" values (returns True for both coordination
        members)."""
        for dai in self.dais:
            if dai.value == value:
                return dai.slot
            if (dai.value is not None and
                    value not in [None, '?'] and
                    (re.match(r'.* (and|or) ' + value + r'$', dai.value) or
                     re.match(r'^' + value + r' (and|or) ', dai.value))):
                return dai.slot
        return None

#
# Morphology & delexicalization


class MorphoAnalyzer(object):

    def __init__(self, tagger_model, abst_slots):
        self._tagger = Tagger.load(tagger_model)
        self._analyzer = self._tagger.getMorpho()
        self._tokenizer = self._tagger.newTokenizer()
        self._abst_slots = set(abst_slots.split(','))

        self._forms_buf = Forms()
        self._tokens_buf = TokenRanges()
        self._analyses_buf = Analyses()
        self._indices_buf = Indices()

        self._sf_dict = {}
        self._sf_max_len = 0

    def load_surface_forms(self, surface_forms_fname):
        """Load all proper name surface forms from a file."""
        with codecs.open(surface_forms_fname, 'rb', 'UTF-8') as fh:
            data = json.load(fh)
        for slot, values in data.iteritems():
            for value in values.keys():
                for surface_form in values[value]:
                    form, tag = surface_form.split("\t")
                    form_toks = form.lower().split(" ")
                    lemma = value
                    if slot == 'street':  # add street number placeholders to addresses
                        lemma += ' _'
                        form_toks.append('_')
                    form_toks = tuple(form_toks)
                    self._sf_max_len = max((self._sf_max_len, len(form_toks)))
                    if form_toks not in self._sf_dict:
                        self._sf_dict[form_toks] = []
                    self._sf_dict[form_toks].append((lemma, tag))

    def _get_surface_form_taggedlemmas(self, forms_in):
        """Given a tokens deque, return the form & list of tagged lemmas (analyses)
        for the proper name in the list of forms at the current position, if applicable.
        If there is no proper name at the beginning of the tokens deque, return (None, None).

        @param forms_in: a deque of forms tokens
        @return: (form, tagged lemmas list) or (None, None)
        """
        for test_len in xrange(min(self._sf_max_len, len(forms_in)), 0, -1):
            # test the string, handle number placeholders
            full_substr = [form for form in islice(forms_in, 0, test_len)]
            test_substr = tuple(['_' if re.match(r'^[0-9]+$', form) else form.lower()
                                 for form in full_substr])
            if test_substr in self._sf_dict:
                tls = TaggedLemmas()
                nums = [num for num in full_substr if re.match(r'^[0-9]+$', num)]
                for lemma, tag in self._sf_dict[test_substr]:
                    tls.push_back(TaggedLemma())
                    for num in nums:  # replace number placeholders by actual values
                        lemma = re.sub(r'_', num, lemma, count=1)
                    tls[-1].lemma = lemma
                    tls[-1].tag = tag
                for _ in xrange(len(test_substr)):  # move on in the sentence
                    forms_in.popleft()
                return " ".join(full_substr), tls
        return None, None

    def analyze(self, sent):
        """Perform morphological analysis on the given sentence, preferring analyses from the
        list of surface forms. Return a list of tuples (form, lemma, tag)."""
        self._tokenizer.setText(sent)
        analyzed = []
        while self._tokenizer.nextSentence(self._forms_buf, self._tokens_buf):

            forms_in = deque(self._forms_buf)
            self._forms_buf.resize(0)
            self._analyses_buf.resize(0)  # reset previous analyses

            while forms_in:
                form, analyses = self._get_surface_form_taggedlemmas(forms_in)
                if form:
                    # our custom analysis
                    self._analyses_buf.push_back(analyses)
                else:
                    # Morphodita analysis
                    form = forms_in.popleft()
                    analyses = TaggedLemmas()
                    self._analyzer.analyze(form, 1, analyses)
                    for i in xrange(len(analyses)):  # shorten lemmas (must access the vector directly)
                        analyses[i].lemma = self._analyzer.rawLemma(analyses[i].lemma)
                    self._analyses_buf.push_back(analyses)

                self._forms_buf.push_back(form)

            # tag according to the given analysis
            self._tagger.tagAnalyzed(self._forms_buf, self._analyses_buf, self._indices_buf)
            analyzed.extend([(f, a[idx].lemma, a[idx].tag)
                             for (f, a, idx)
                             in zip(self._forms_buf, self._analyses_buf, self._indices_buf)])
        return analyzed

    def get_delex_instr(self, analyzed, da):
        """Get delexicalization/abstraction instructions from the given analyzed sentence
        and the given corresponding DA. All values in the analyzed sentence are assumed to
        be single-token (which may contains spaces)."""
        absts = []
        for idx, (_, lemma, _) in enumerate(analyzed):
            slot = da.has_value(lemma)
            if slot:
                absts.append(Abst(slot, lemma, idx, idx+1))
        return absts

    def process_files(self, input_text_file, input_da_file):
        """Load DAs & sentences, obtain abstraction instructions, and store it all in member
        variables (to be used later by writing methods)."""
        # load DAs
        self._das = []
        with codecs.open(input_da_file, 'r', encoding='UTF-8') as fh:
            for line in fh:
                self._das.append(DA.parse(line.strip()))
        # load & process sentences
        self._sents = []
        with codecs.open(input_text_file, 'r', encoding='UTF-8') as fh:
            for line in fh:
                self._sents.append(self.analyze(line.strip()))
        assert(len(self._das) == len(self._sents))
        # get abstraction instructions for all sentence-DA pairs
        self._absts = [self.get_delex_instr(sent, da) for sent, da in zip(self._sents, self._das)]

    def buf_length(self):
        """Return the number of sentence-DA pairs currently loaded in the buffer."""
        return len(self._sents)

    def _write_plain(self, output_file, data_items):
        with codecs.open(output_file, 'wb', encoding='UTF-8') as fh:
            for data_item in data_items:
                print >> fh, unicode(data_item)

    def _write_conll(self, output_file, data_items):
        with codecs.open(output_file, 'wb', encoding='UTF-8') as fh:
            for line in data_items:
                for idx, tok in enumerate(line, start=1):
                    print >> fh, "\t".join((str(idx),
                                            tok[0].replace(' ', '_'),
                                            tok[1].replace(' ', '_'),
                                            '_', tok[2], '_',
                                            '0', '_', '_', '_'))
                print >> fh

    def _write_interleaved(self, output_file, data_items):
        with codecs.open(output_file, 'wb', encoding='UTF-8') as fh:
            for line in data_items:
                for _, lemma, tag in line:
                    print >> fh, lemma.replace(' ', '_'), tag,
                print >> fh

    def write_text(self, data_file, out_format, subrange, delex=False):
        """Write output sentences for the given data subrange.
        @param data_file: output file name
        @param out_format: output format ('conll' -- CoNLL-U morphology, \
            'interleaved' -- lemma/tag interleaved, 'plain' -- plain text)
        @param subrange: data range (slice) from buffers to write
        @param delex: delexicalize? false by default
        """
        if delex:
            texts = self._delex_texts(subrange)
        else:
            texts = self._sents[subrange]
        if out_format == 'interleaved':
            self._write_interleaved(data_file, texts)
        elif out_format == 'conll':
            self._write_conll(data_file, texts)
        else:
            self._write_plain(data_file, [" ".join([form for form, _, _ in sent])
                                          for sent in texts])

    def write_absts(self, data_file, subrange):
        """Write delexicalization/abstraction instructions (for the given data subrange).
        @param data_file: output file name
        @param subrange: data range (slice) from buffers to write
        """
        self._write_plain(data_file, ["\t".join([unicode(abst_) for abst_ in abst])
                                      for abst in self._absts[subrange]])

    def write_das(self, data_file, subrange, delex=False):
        """Write DAs (for the given subrange).
        @param data_file: output file name
        @param subrange: data range (slice) from buffers to write
        @param delex: delexicalize? false by default
        """
        if delex:
            das = self._delex_das(subrange)
        else:
            das = self._das[subrange]
        self._write_plain(data_file, das)

    def _delex_das(self, subrange):
        """Delexicalize DAs in the given subrange (slice) of the buffers."""
        out = []
        for da in self._das[subrange]:
            delex_da = DA()
            for dai in da:
                delex_dai = DAI(dai.type, dai.slot,
                                'X-' + dai.slot
                                if (dai.value and dai.slot in self._abst_slots)
                                else dai.value)
                delex_da.append(delex_dai)
            out.append(delex_da)
        return out

    def _delex_texts(self, subrange):
        """Delexicalize texts in the given subrange (slice) of the buffers."""
        out = []
        for idx, (text, da) in enumerate(zip(self._sents[subrange], self._das[subrange]),
                                         start=subrange.start):
            delex_text = []
            covered_slots = set()
            # do the delexicalization, keep track of which slots we used
            for (form, lemma, tag) in text:
                slot = da.has_value(lemma)
                if slot and slot in self._abst_slots:
                    delex_text.append(('X-' + slot, 'X-' + slot, tag))
                    covered_slots.add(slot)
                else:
                    delex_text.append((form, lemma, tag))
            # check and warn if we left something non-delexicalized
            for dai in da:
                if (dai.slot in self._abst_slots and
                        dai.value not in [None, 'none', 'dont_care'] and
                        dai.slot not in covered_slots):
                    log_info("Cannot delexicalize slot  %s  at %d:\nDA: %s\nTx: %s\n" %
                             (dai.slot, idx, unicode(da), " ".join([form for form, _, _ in text])))
            # output the delexicalized text
            out.append(delex_text)
        return out


def convert(args):
    """Main conversion function (using command-line arguments as parsed by Argparse)."""
    log_info('Loading...')
    analyzer = MorphoAnalyzer(args.tagger_model, args.abst_slots)
    analyzer.load_surface_forms(args.surface_forms)
    log_info('Processing input files...')
    analyzer.process_files(args.input_text_file, args.input_da_file)
    log_info('Loaded %d data items.' % analyzer.buf_length())

    # outputs: plain delex, plain lex, interleaved delex & lex, CoNLL-U delex & lex, DAs, abstrs
    # TODO maybe do relexicalization, but not now (no time)

    if args.split:
        # get file name prefixes and compute data sizes for all the parts to be split
        out_names = re.split(r'[, ]+', args.out_prefix)
        data_sizes = [int(part_size) for part_size in args.split.split(':')]
        assert len(out_names) == len(data_sizes)
        # compute sizes for all but the 1st part (+ round them)
        total = float(sum(data_sizes))
        remain = analyzer.buf_length()
        for part_no in xrange(len(data_sizes) - 1, 0, -1):
            part_size = int(round(analyzer.buf_length() * (data_sizes[part_no] / total)))
            data_sizes[part_no] = part_size
            remain -= part_size
        # put whatever remained into the 1st part
        data_sizes[0] = remain
    else:
        # use just one part -- containing all the data
        data_sizes = [analyzer.buf_length()]
        out_names = [args.out_prefix]

    # write all data parts
    offset = 0
    for part_size, part_name in zip(data_sizes, out_names):
        log_info('Writing %s (size: %d)...' % (part_name, part_size))
        subrange = slice(offset, offset + part_size)

        analyzer.write_absts(part_name + '-abst.txt', subrange)

        analyzer.write_das(part_name + '-das_l.txt', subrange)
        analyzer.write_das(part_name + '-das.txt', subrange, delex=True)

        analyzer.write_text(part_name + '-text_l.txt', 'plain', subrange)
        analyzer.write_text(part_name + '-text.txt', 'plain', subrange, delex=True)
        analyzer.write_text(part_name + '-tls_l.txt', 'interleaved', subrange)
        analyzer.write_text(part_name + '-tls.txt', 'interleaved', subrange, delex=True)
        analyzer.write_text(part_name + '-text_l.conll', 'conll', subrange)
        analyzer.write_text(part_name + '-text.conll', 'conll', subrange, delex=True)

        offset += part_size


if __name__ == '__main__':
    ap = ArgumentParser()

    ap.add_argument('tagger_model', type=str, help='MorphoDiTa tagger model')
    ap.add_argument('surface_forms', type=str, help='Input JSON with base forms')
    ap.add_argument('input_da_file', type=str, help='Input DA file')
    ap.add_argument('input_text_file', type=str, help='Input text file')
    ap.add_argument('out_prefix', help='Output files name prefix(es - when used with -s, comma-separated)')
    ap.add_argument('-a', '--abst-slots', help='List of slots to delexicalize/abstract (comma-separated)')
    ap.add_argument('-s', '--split', help='Colon-separated sizes of splits (e.g.: 3:1:1)')

    args = ap.parse_args()
    convert(args)
