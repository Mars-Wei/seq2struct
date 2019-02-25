import itertools
import operator

import numpy as np
import torch
from torch import nn

from seq2struct.models import lstm
from seq2struct.models import transformer
from seq2struct.utils import batched_sequence


def clamp(value, abs_max):
    value = max(-abs_max, value)
    value = min(abs_max, value)
    return value


def get_attn_mask(seq_lengths):
    # Given seq_lengths like [3, 1, 2], this will produce
    # [[1, 1, 1],
    #  [1, 0, 0],
    #  [1, 1, 0]]
    # int(max(...)) so that it has type 'int instead of numpy.int64
    max_length, batch_size = int(max(seq_lengths)), len(seq_lengths)
    ranges = torch.arange(
        0, max_length,
        out=torch.LongTensor()).unsqueeze(0).expand(batch_size, -1)
    attn_mask = (ranges < torch.LongTensor(seq_lengths).unsqueeze(1))
    return attn_mask


class LookupEmbeddings(torch.nn.Module):
    def __init__(self, device, vocab, emb_size):
        super().__init__()
        self._device = device
        self.vocab = vocab

        self.emb_size = emb_size
        self.embedding = torch.nn.Embedding(
            num_embeddings=len(self.vocab), embedding_dim=emb_size)

    def forward(self, token_lists):
        # token_lists: list of list of lists
        # [batch, num descs, desc length]
        # - each list contains tokens
        # - each list corresponds to a column name, table name, etc.

        # PackedSequencePlus, with shape: [batch, num descs * desc length (sum of desc lengths)]
        indices = batched_sequence.PackedSequencePlus.from_lists(
            lists=[
                [
                    token 
                    for token_list in token_lists_for_item
                    for token in token_list
                ]
                for token_lists_for_item in token_lists
            ],
            item_shape=(1,),  # For 
            tensor_type=torch.LongTensor,
            item_to_tensor=lambda token, batch_idx, out: out.fill_(self.vocab.index(token))
        )
        # PackedSequencePlus, with shape: [batch, sum of desc lengths, emb_size]
        all_embs = indices.apply(lambda x: self.embedding(x.squeeze(-1)))

        # boundaries shape: [batch, num descs + 1]
        boundaries = [
            np.cumsum([0] + [len(token_list) for token_list in token_lists_for_item])
            for token_lists_for_item in token_lists]

        return all_embs, boundaries


class BiLSTM(torch.nn.Module):
    def __init__(self, input_size, output_size, dropout, summarize):
        # input_size: dimensionality of input
        # output_size: dimensionality of output
        # dropout
        # summarize:
        # - True: return Tensor of 1 x batch x emb size 
        # - False: return Tensor of seq len x batch x emb size 
        super().__init__()

        self.lstm = lstm.LSTM(
                input_size=input_size,
                hidden_size=output_size // 2,
                bidirectional=True,
                dropout=dropout)
        self.summarize = summarize

    def forward(self, input_):
        # all_embs shape: PackedSequencePlus with shape [batch, sum of desc lengths, input_size]
        # boundaries: list of lists with shape [batch, num descs + 1]
        all_embs, boundaries = input_

        # List of the following:
        # (batch_idx, desc_idx, length)
        desc_lengths = []
        batch_desc_to_flat_map = {}
        for batch_idx, boundaries_for_item in enumerate(boundaries):
            for desc_idx, (left, right) in enumerate(zip(boundaries_for_item, boundaries_for_item[1:])):
                desc_lengths.append((batch_idx, desc_idx, right - left))
                batch_desc_to_flat_map[batch_idx, desc_idx] = len(batch_desc_to_flat_map)
        
        # Recreate PackedSequencePlus into shape
        # [batch * num descs, desc length, input_size]
        # with name `rearranged_all_embs`
        remapped_ps_indices = []
        def rearranged_all_embs_map_index(desc_lengths_idx, seq_idx):
            batch_idx, desc_idx, _ = desc_lengths[desc_lengths_idx]
            return batch_idx, boundaries[batch_idx][desc_idx] + seq_idx
        def rearranged_all_embs_gather_from_indices(indices):
            batch_indices, seq_indices = zip(*indices)
            remapped_ps_indices[:] =  all_embs.raw_index(batch_indices, seq_indices)
            return all_embs.ps.data[torch.LongTensor(remapped_ps_indices)]
        rearranged_all_embs = batched_sequence.PackedSequencePlus.from_gather(
            lengths=[length for _, _, length in desc_lengths],
            map_index=rearranged_all_embs_map_index,
            gather_from_indices=rearranged_all_embs_gather_from_indices)
        rev_remapped_ps_indices = tuple(
            x[0] for x in sorted(
                enumerate(remapped_ps_indices), key=operator.itemgetter(1)))
        
        ## Sort desc_lengths in terms of decreasing length
        #sorted_desc_lengths, desc_sort_to_orig, desc_orig_to_sort = batched_sequence.argsort(
        #        desc_lengths, key=operator.itemgetter(2), reverse=True)

        ## Recreate PackedSequencePlus into shape
        ## [batch * num descs, desc length, input_size]
        ## with name `rearranged_all_embs`
        #lengths = [length for _, _, length in sorted_desc_lengths]
        #batch_bounds = batched_sequence.batch_bounds_for_packing(lengths)
        #batch_indices, seq_indices = [], []
        #for seq_idx, bound in enumerate(batch_bounds):
        #    for batch_idx, desc_idx, _ in sorted_desc_lengths[:bound]:
        #        batch_indices.append(batch_idx)
        #        seq_indices.append(boundaries[batch_idx][desc_idx] + seq_idx)
        ## [1, 3, 2, 0]
        #remapped_ps_indices = all_embs.raw_index(batch_indices, seq_indices)
        ## [3, 0, 2, 1]
        #rev_remapped_ps_indices = tuple(
        #    x[0] for x in sorted(
        #        enumerate(remapped_ps_indices), key=operator.itemgetter(1)))
        #rearranged_all_embs = batched_sequence.PackedSequencePlus(
        #    torch.nn.utils.rnn.PackedSequence(
        #        all_embs.ps.data[torch.LongTensor(remapped_ps_indices)],
        #        batch_bounds),
        #    lengths, desc_sort_to_orig, desc_orig_to_sort)
        
        # output shape: PackedSequence, [batch * num_descs, desc length, output_size]
        # state shape:
        # - h: [num_layers (=1) * num_directions (=2), batch, output_size / 2]
        # - c: [num_layers (=1) * num_directions (=2), batch, output_size / 2]
        output, (h, c) = self.lstm(rearranged_all_embs.ps)
        if self.summarize:
            # h shape: [batch * num descs, output_size]
            h = torch.cat((h[0], h[1]), dim=-1)

            # new_all_embs: PackedSequencePlus, [batch, num descs, input_size]
            new_all_embs = batched_sequence.PackedSequencePlus.from_gather(
                lengths=[len(boundaries_for_item) - 1 for boundaries_for_item in boundaries],
                map_index=lambda batch_idx, desc_idx: rearranged_all_embs.orig_to_sort[batch_desc_to_flat_map[batch_idx, desc_idx]],
                gather_from_indices=lambda indices: h[torch.LongTensor(indices)])

            new_boundaries = [
                list(range(len(boundaries_for_item)))
                for boundaries_for_item in boundaries
            ]
        else:
            new_all_embs = all_embs.apply(
                lambda _: output.data[torch.LongTensor(rev_remapped_ps_indices)])
            new_boundaries = boundaries
        
        return new_all_embs, new_boundaries


class RelationalTransformerUpdate(torch.nn.Module):

    def __init__(self, device, num_layers, num_heads, hidden_size, 
            ff_size=None,
            dropout=0.1,
            qq_max_dist=2,
            #qc_token_match=True,
            #qt_token_match=True,
            #cq_token_match=True,
            cc_foreign_key=True,
            cc_table_match=True,
            cc_max_dist=2,
            ct_foreign_key=True,
            ct_table_match=True,
            #tq_token_match=True,
            tc_table_match=True,
            tc_foreign_key=True,
            tt_max_dist=2,
            tt_foreign_key=True,
            ):
        super().__init__()
        self._device = device

        self.qq_max_dist    = qq_max_dist
        #self.qc_token_match = qc_token_match
        #self.qt_token_match = qt_token_match
        #self.cq_token_match = cq_token_match
        self.cc_foreign_key = cc_foreign_key
        self.cc_table_match = cc_table_match
        self.cc_max_dist    = cc_max_dist
        self.ct_foreign_key = ct_foreign_key
        self.ct_table_match = ct_table_match
        #self.tq_token_match = tq_token_match
        self.tc_table_match = tc_table_match
        self.tc_foreign_key = tc_foreign_key
        self.tt_max_dist    = tt_max_dist
        self.tt_foreign_key = tt_foreign_key

        self.relation_ids = {}
        def add_relation(name):
            self.relation_ids[name] = len(self.relation_ids)
        def add_rel_dist(name, max_dist):
            for i in range(-max_dist, max_dist + 1):
                add_relation((name, i))

        add_rel_dist('qq_dist', qq_max_dist)

        add_relation('qc_default')
        #if qc_token_match:
        #    add_relation('qc_token_match')

        add_relation('qt_default')
        #if qt_token_match:
        #    add_relation('qt_token_match')

        add_relation('cq_default')
        #if cq_token_match:
        #    add_relation('cq_token_match')

        add_relation('cc_default')
        if cc_foreign_key:
            add_relation('cc_foreign_key_forward')
            add_relation('cc_foreign_key_backward')
        if cc_table_match:
            add_relation('cc_table_match')
        add_rel_dist('cc_dist', cc_max_dist)

        add_relation('ct_default')
        if ct_foreign_key:
            add_relation('ct_foreign_key')
        if ct_table_match:
            add_relation('ct_primary_key')
            add_relation('ct_table_match')
            add_relation('ct_any_table')

        add_relation('tq_default')
        #if cq_token_match:
        #    add_relation('tq_token_match')

        add_relation('tc_default')
        if tc_table_match:
            add_relation('tc_primary_key')
            add_relation('tc_table_match')
            add_relation('tc_any_table')
        if tc_foreign_key:
            add_relation('tc_foreign_key')

        add_relation('tt_default')
        if tt_foreign_key:
            add_relation('tt_foreign_key_forward')
            add_relation('tt_foreign_key_backward')
            add_relation('tt_foreign_key_both')
        add_rel_dist('tt_dist', tt_max_dist)

        if ff_size is None:
            ff_size = hidden_size * 4
        self.encoder = transformer.Encoder(
            lambda: transformer.EncoderLayer(
                hidden_size, 
                transformer.MultiHeadedAttentionWithRelations(
                    num_heads,
                    hidden_size,
                    dropout),
                transformer.PositionwiseFeedForward(
                    hidden_size,
                    ff_size,
                    dropout),
                len(self.relation_ids),
                dropout),
            hidden_size,
            num_layers)
    
    def forward(self, descs, q_enc, c_enc, c_boundaries, t_enc, t_boundaries):
        # enc: PackedSequencePlus with shape [batch, total len, recurrent size]
        enc = batched_sequence.PackedSequencePlus.cat_seqs((q_enc, c_enc, t_enc))

        q_enc_lengths = list(q_enc.orig_lengths())
        c_enc_lengths = list(c_enc.orig_lengths())
        t_enc_lengths = list(t_enc.orig_lengths())
        enc_lengths = list(enc.orig_lengths())
        max_enc_length = max(enc_lengths)

        all_relations = []
        for batch_idx, desc in enumerate(descs):
            enc_length = enc_lengths[batch_idx]
            relations_for_item = self.compute_relations(
                desc,
                enc_length,
                q_enc_lengths[batch_idx],
                c_enc_lengths[batch_idx],
                c_boundaries[batch_idx],
                t_boundaries[batch_idx])
            all_relations.append(np.pad(relations_for_item, ((0, max_enc_length - enc_length),), 'constant'))
        relations_t = torch.from_numpy(np.stack(all_relations)).to(self._device)

        # mask shape: [batch, total len, 1]
        # the last dimension will get broadcasted
        mask = get_attn_mask(enc_lengths).unsqueeze(-1).to(self._device)
        # enc_new: shape [batch, total len, recurrent size]
        enc_padded, _ = enc.pad(batch_first=True)
        enc_new = self.encoder(enc_padded, relations_t, mask=mask) 

        # Split enc_new again
        def gather_from_enc_new(indices):
            batch_indices, seq_indices = zip(*indices)
            return enc_new[torch.LongTensor(batch_indices), torch.LongTensor(seq_indices)]

        q_enc_new = batched_sequence.PackedSequencePlus.from_gather(
            lengths=q_enc_lengths,
            map_index=lambda batch_idx, seq_idx: (batch_idx, seq_idx),
            gather_from_indices=gather_from_enc_new)
        c_enc_new = batched_sequence.PackedSequencePlus.from_gather(
            lengths=c_enc_lengths,
            map_index=lambda batch_idx, seq_idx: (batch_idx, q_enc_lengths[batch_idx] + seq_idx),
            gather_from_indices=gather_from_enc_new)
        t_enc_new = batched_sequence.PackedSequencePlus.from_gather(
            lengths=t_enc_lengths,
            map_index=lambda batch_idx, seq_idx: (batch_idx, q_enc_lengths[batch_idx] + c_enc_lengths[batch_idx] + seq_idx),
            gather_from_indices=gather_from_enc_new)
        return q_enc_new, c_enc_new, t_enc_new

    def compute_relations(self, desc, enc_length, q_enc_length, c_enc_length, c_boundaries, t_boundaries):
        # Catalogue which things are where
        loc_types = {}
        for i in range(q_enc_length):
            loc_types[i] = ('question',)

        c_base = q_enc_length
        for c_id, (c_start, c_end) in enumerate(zip(c_boundaries, c_boundaries[1:])):
            for i in range(c_start + c_base, c_end + c_base):
                loc_types[i] = ('column', c_id)
        t_base = q_enc_length + c_enc_length
        for t_id, (t_start, t_end) in enumerate(zip(t_boundaries, t_boundaries[1:])):
            for i in range(t_start + t_base, t_end + t_base):
                loc_types[i] = ('table', t_id)
        
        relations = np.empty((enc_length, enc_length), dtype=np.int64)

        for i, j in itertools.product(range(enc_length),repeat=2):
            def set_relation(name):
                relations[i, j] = self.relation_ids[name]

            i_type, j_type = loc_types[i], loc_types[j]
            if i_type[0] == 'question':
                if j_type[0] == 'question':
                    set_relation(('qq_dist', clamp(j - i, self.qq_max_dist)))
                elif j_type[0] == 'column':
                    set_relation('qc_default')
                elif j_type[0] == 'table':
                    set_relation('qt_default')

            elif i_type[0] == 'column':
                if j_type[0] == 'question':
                    set_relation('cq_default')
                elif j_type[0] == 'column':
                    col1, col2 = i_type[1], j_type[1]
                    if col1 == col2:
                        set_relation(('cc_dist', clamp(j - i, self.cc_max_dist)))
                    else:
                        set_relation('cc_default')
                        if self.cc_foreign_key:
                            if desc['foreign_keys'].get(str(col1)) == col2:
                                set_relation('cc_foreign_key_forward')
                            if desc['foreign_keys'].get(str(col2)) == col1:
                                set_relation('cc_foreign_key_backward')
                        if (self.cc_table_match and 
                            desc['column_to_table'][str(col1)] == desc['column_to_table'][str(col2)]):
                            set_relation('cc_table_match')

                elif j_type[0] == 'table':
                    col, table = i_type[1], j_type[1]
                    set_relation('ct_default')
                    if self.ct_foreign_key and self.match_foreign_key(desc, col, table):
                        set_relation('ct_foreign_key')
                    if self.ct_table_match:
                        col_table = desc['column_to_table'][str(col)] 
                        if col_table == table:
                            if col in desc['primary_keys']:
                                set_relation('ct_primary_key')
                            else:
                                set_relation('ct_table_match')
                        elif col_table is None:
                            set_relation('ct_any_table')

            elif i_type[0] == 'table':
                if j_type[0] == 'question':
                    set_relation('tq_default')
                elif j_type[0] == 'column':
                    table, col = i_type[1], j_type[1]
                    set_relation('tc_default')

                    if self.tc_foreign_key and self.match_foreign_key(desc, col, table):
                        set_relation('tc_foreign_key')
                    if self.tc_table_match:
                        col_table = desc['column_to_table'][str(col)] 
                        if col_table == table:
                            if col in desc['primary_keys']:
                                set_relation('tc_primary_key')
                            else:
                                set_relation('tc_table_match')
                        elif col_table is None:
                            set_relation('tc_any_table')
                elif j_type[0] == 'table':
                    table1, table2 = i_type[1], j_type[1]
                    if table1 == table2:
                        set_relation(('tt_dist', clamp(j - i, self.tt_max_dist)))
                    else:
                        set_relation('tt_default')
                        if self.tt_foreign_key:
                            forward = table2 in desc['foreign_keys_tables'].get(str(table1), ())
                            backward = table1 in desc['foreign_keys_tables'].get(str(table2), ())
                            if forward and backward:
                                set_relation('tt_foreign_key_both')
                            elif forward:
                                set_relation('tt_foreign_key_forward')
                            elif backward:
                                set_relation('tt_foreign_key_backward')
        return relations

    @classmethod
    def match_foreign_key(cls, desc, col, table):
        foreign_key_for = desc['foreign_keys'].get(str(col))
        if foreign_key_for is None:
            return False

        foreign_table = desc['column_to_table'][str(foreign_key_for)]
        return desc['column_to_table'][str(col)] == foreign_table


class NoOpUpdate:
    def __init__(self, device, hidden_size):
        pass

    def __call__(self, desc, q_enc, c_enc, c_boundaries, t_enc, t_boundaries):
        return q_enc.transpose(0, 1), c_enc.transpose(0, 1), t_enc.transpose(0, 1)
