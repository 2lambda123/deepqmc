import haiku as hk
import jax.numpy as jnp
from jax import ops
from jax.tree_util import tree_map

from ...hkext import MLP
from .distbasis import DistanceBasis
from .graph import Graph, GraphNodes, MessagePassingLayer


class DiffSchNetLayer(MessagePassingLayer):
    def __init__(
        self,
        name,
        ilayer,
        embedding_dim,
        kernel_dim,
        dist_feat_dim,
        distance_basis,
        shared_h=True,
        shared_g=False,
        w_subnet=None,
        h_subnet=None,
        g_subnet=None,
        *,
        n_layers_w=2,
        n_layers_h=1,
        n_layers_g=1,
    ):
        super().__init__(name, ilayer)

        def default_subnet_kwargs(n_layers):
            return {
                'hidden_layers': ('log', n_layers),
                'last_bias': False,
                'last_linear': True,
            }

        labels = ['same', 'anti', 'ne']
        self.w = {
            lbl: MLP(
                dist_feat_dim,
                kernel_dim,
                name=f'w_{lbl}',
                **(w_subnet or default_subnet_kwargs(n_layers_w)),
            )
            for lbl in labels
        }
        self.h = (
            MLP(
                embedding_dim,
                kernel_dim,
                name='h',
                **(h_subnet or default_subnet_kwargs(n_layers_h)),
            )
            if shared_h
            else {
                lbl: MLP(
                    embedding_dim,
                    kernel_dim,
                    name=f'h_{lbl}',
                    **(h_subnet or default_subnet_kwargs(n_layers_h)),
                )
                for lbl in labels
            }
        )
        self.g = (
            MLP(
                kernel_dim,
                embedding_dim,
                name='g',
                **(g_subnet or default_subnet_kwargs(n_layers_g)),
            )
            if shared_g
            else {
                lbl: MLP(
                    kernel_dim,
                    embedding_dim,
                    name=f'g_{lbl}',
                    **(g_subnet or default_subnet_kwargs(n_layers_g)),
                )
                for lbl in labels
            }
        )
        self.distance_basis = distance_basis
        self.labels = labels
        self.shared_h = shared_h
        self.shared_g = shared_g

    def expand_diffs(self, diffs):
        diffs_expanded = []
        for i, diff in enumerate(diffs.T):
            if i < 3:
                diff_pos = jnp.abs(diff) * (diff > 0)
                diff_neg = jnp.abs(diff) * (diff < 0)
                diffs_expanded.append(self.distance_basis(diff_pos))
                diffs_expanded.append(self.distance_basis(diff_neg))
            else:
                diffs_expanded.append(self.distance_basis(diff))
        return jnp.concatenate(diffs_expanded, axis=-1)

    def get_update_edges_fn(self):
        def update_edges_fn(nodes, edges):
            expanded = edges._replace(data=tree_map(self.expand_diffs, edges.data))
            return expanded

        return update_edges_fn if self.ilayer == 0 else None

    def get_aggregate_edges_for_nodes_fn(self):
        def aggregate_edges_for_nodes_fn(nodes, edges):
            n_elec = nodes.electrons.shape[-2]
            we_same, we_anti, we_n = (
                self.w[lbl](edges.data['diffs'][lbl]) for lbl in self.labels
            )
            hx_same, hx_anti = (
                (self.h if self.shared_h else self.h[lbl])(
                    nodes.electrons[edges.senders[lbl]]
                )
                for lbl in self.labels[:2]
            )
            weh_same = we_same * hx_same
            weh_anti = we_anti * hx_anti
            weh_n = we_n * nodes.nuclei[edges.senders['ne']]
            z_same = ops.segment_sum(
                data=weh_same, segment_ids=edges.receivers['same'], num_segments=n_elec
            )
            z_anti = ops.segment_sum(
                data=weh_anti, segment_ids=edges.receivers['anti'], num_segments=n_elec
            )
            z_n = ops.segment_sum(
                data=weh_n, segment_ids=edges.receivers['ne'], num_segments=n_elec
            )
            return {
                'same': z_same,
                'anti': z_anti,
                'ne': z_n,
            }

        return aggregate_edges_for_nodes_fn

    def get_update_nodes_fn(self):
        def update_nodes_fn(nodes, z):
            updated_nodes = nodes._replace(
                electrons=nodes.electrons
                + (
                    (self.g if self.shared_g else self.g['ne'])(z['ne'])
                    + (self.g if self.shared_g else self.g['same'])(z['same'])
                    + (self.g if self.shared_g else self.g['anti'])(z['anti'])
                )
            )
            return updated_nodes

        return update_nodes_fn


class DiffSchNet(hk.Module):
    def __init__(
        self,
        n_nuc,
        n_up,
        n_down,
        coords,
        embedding_dim,
        dist_feat_dim=32,
        kernel_dim=128,
        n_interactions=3,
        cutoff=10.0,
        layer_kwargs=None,
    ):
        super().__init__('SchNet')
        self.coords = coords
        elec_vocab_size = 1 if n_up == n_down else 2
        self.spin_idxs = jnp.array(
            (n_up + n_down) * [0] if n_up == n_down else n_up * [0] + n_down * [1]
        )
        self.X = hk.Embed(elec_vocab_size, embedding_dim, name='ElectronicEmbedding')
        self.Y = hk.Embed(n_nuc, kernel_dim, name='NuclearEmbedding')
        self.nuclei_idxs = jnp.arange(n_nuc)
        self.layers = [
            DiffSchNetLayer(
                'DiffSchNetLayer',
                i,
                embedding_dim,
                kernel_dim,
                7 * dist_feat_dim,
                DistanceBasis(dist_feat_dim, cutoff, envelope='nocusp')
                if i == 0
                else None,
                **(layer_kwargs or {}),
            )
            for i in range(n_interactions)
        ]

    @classmethod
    def required_edge_types(cls):
        return ['ne', 'same', 'anti']

    def __call__(self, rs, graph_edges):
        def compute_distances(labels, positions):
            def diff(senders, receivers):
                diffs = receivers - senders
                return jnp.concatenate(
                    [diffs, jnp.sqrt((diffs**2).sum(axis=-1, keepdims=True))], axis=-1
                )

            data = {
                'diffs': {
                    lbl: diff(
                        pos[0][graph_edges.senders[lbl]],
                        pos[1][graph_edges.receivers[lbl]],
                    )
                    for lbl, pos in zip(labels, positions)
                }
            }
            return data

        nuc_embedding = self.Y(self.nuclei_idxs)
        elec_embedding = self.X(self.spin_idxs)
        graph = Graph(
            GraphNodes(nuc_embedding, elec_embedding),
            graph_edges._replace(
                data=compute_distances(
                    ['ne', 'same', 'anti'], [(self.coords, rs), (rs, rs), (rs, rs)]
                )
            ),
        )
        for layer in self.layers:
            graph = layer(graph)
        return graph.nodes.electrons