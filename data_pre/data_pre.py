from torch_geometric.data import Data
from collections import defaultdict
from sklearn.model_selection import StratifiedShuffleSplit
from rdkit import Chem
import pandas as pd
from rdkit.Chem import AllChem
from rdkit import DataStructs
from tqdm import tqdm

import torch
import pickle

import torch.utils.data
import os

import dgl

from scipy import sparse as sp
import numpy as np

class CustomData(Data):
    def __inc__(self, key, value, *args, **kwargs):
        if key == 'line_graph_edge_index':
            return self.edge_index.size(1) if self.edge_index.nelement() != 0 else 0
        return super().__inc__(key, value, *args, **kwargs)


def one_of_k_encoding(k, possible_values):
    if k not in possible_values:
        raise ValueError(f"{k} is not a valid value in {possible_values}")
    return [k == e for e in possible_values]


def one_of_k_encoding_unk(x, allowable_set):
    if x not in allowable_set:
        x = allowable_set[-1]
    return list(map(lambda s: x == s,
                    allowable_set))

def atom_features(atom, atom_symbols, explicit_H=True, use_chirality=False):
    results = one_of_k_encoding_unk(atom.GetSymbol(), atom_symbols + ['Unknown']) + \
              one_of_k_encoding(atom.GetDegree(), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) + \
              one_of_k_encoding_unk(atom.GetImplicitValence(), [0, 1, 2, 3, 4, 5, 6]) + \
              [atom.GetFormalCharge(), atom.GetNumRadicalElectrons()] + \
              one_of_k_encoding_unk(atom.GetHybridization(), [
                  Chem.rdchem.HybridizationType.SP, Chem.rdchem.HybridizationType.SP2,
                  Chem.rdchem.HybridizationType.SP3, Chem.rdchem.HybridizationType.
                                    SP3D, Chem.rdchem.HybridizationType.SP3D2
              ]) + [atom.GetIsAromatic()]
    if explicit_H:
        results = results + one_of_k_encoding_unk(atom.GetTotalNumHs(),
                                                  [0, 1, 2, 3, 4])
    if use_chirality:
        try:
            results = results + one_of_k_encoding_unk(
                atom.GetProp('_CIPCode'),
                ['R', 'S']) + [atom.HasProp('_ChiralityPossible')]
        except:
            results = results + [False, False
                                 ] + [atom.HasProp('_ChiralityPossible')]

    results = np.array(results).astype(np.float32)

    return torch.from_numpy(results)


def edge_features(bond):
    bond_type = bond.GetBondType()
    return torch.tensor([
        bond_type == Chem.rdchem.BondType.SINGLE,
        bond_type == Chem.rdchem.BondType.DOUBLE,
        bond_type == Chem.rdchem.BondType.TRIPLE,
        bond_type == Chem.rdchem.BondType.AROMATIC,
        bond.GetIsConjugated(),#判断化学键是否处于共轭体系中
        bond.IsInRing()]).long()#是否位于环中
##返回每个原子特征x（33，70）、边对edge_index（2，74）,有边相连矩阵中坐标line_graph_edge_index（2，104）、边特征edge_attr=[74,6]、相似矩阵（1，544）,id='DB09053'
def generate_drug_data(mol_graph, atom_symbols, smiles_rdkit_list,id,smile):
    edge_list = torch.LongTensor(
        [(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), *edge_features(b)) for b in mol_graph.GetBonds()])

    edge_list, edge_feats = (edge_list[:, :2], edge_list[:, 2:].float()) if len(edge_list) else (
    torch.LongTensor([]), torch.FloatTensor([]))#将点到点的边序号和对应键分别存储在两个列表中。
    edge_list = torch.cat([edge_list, edge_list[:, [1, 0]]], dim=0) if len(edge_list) else edge_list#添加逆向边（变成两倍长度）
    edge_feats = torch.cat([edge_feats] * 2, dim=0) if len(edge_feats) else edge_feats#两倍长度。

    features = [(atom.GetIdx(), atom_features(atom, atom_symbols)) for atom in mol_graph.GetAtoms()]
    features.sort()
    _, features = zip(*features)
    features = torch.stack(features)#将特征堆叠成一个张量

    line_graph_edge_index = torch.LongTensor([])#构建（edge_list，edge_list）的矩阵，如果两个点相连则为true
    if edge_list.nelement() != 0:
        conn = (edge_list[:, 1].unsqueeze(1) == edge_list[:, 0].unsqueeze(0)) & (
                    edge_list[:, 0].unsqueeze(1) != edge_list[:, 1].unsqueeze(0))
        line_graph_edge_index = conn.nonzero(as_tuple=False).T#找到为true的元素，返回这些位置的坐标（第一个为（2，104）代表（1，0）和（2，0）两个点有边相连）

    new_edge_index = edge_list.T
    fps = [AllChem.GetMorganFingerprintAsBitVect(mol, 2) for mol in smiles_rdkit_list]#smiles对象 mol 和半径 2，表示生成 Morgan 指纹时使用的环的半径
    mol_graph_fps = AllChem.GetMorganFingerprintAsBitVect(mol_graph, 2)#生成分子图的摩根指纹，使用环的半径
    similarity_matrix = np.zeros((1, len(smiles_rdkit_list)))
    for i in range(len(smiles_rdkit_list)):
        similarity = DataStructs.FingerprintSimilarity(fps[i], mol_graph_fps)
        similarity_matrix[0][i] = similarity
    similarity_matrix = torch.tensor(similarity_matrix)

    data = CustomData(x=features, edge_index=new_edge_index, line_graph_edge_index=line_graph_edge_index,
                      edge_attr=edge_feats, sim=similarity_matrix, id=id,smile = smile)

#    data_dgl = {'num_atom': features.shape[0], 'atom_type': features.long(), 'bond_type': edge_feats.long(), 'graph' : g}
    return data#返回每个原子特征x（33，70）、边对edge_index（2，74）,有边相连矩阵中坐标line_graph_edge_index（2，104）、边特征edge_attr=[74,6]、相似矩阵（1，544）,id='DB09053'

def generate_drug_data_dgl(mol_graph, atom_symbols):
    edge_list = torch.LongTensor(
        [(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), *edge_features(b)) for b in mol_graph.GetBonds()])
    edge_list, edge_feats = (edge_list[:, :2], edge_list[:, 2:].float()) if len(edge_list) else (
    torch.LongTensor([]), torch.FloatTensor([]))
    edge_list = torch.cat([edge_list, edge_list[:, [1, 0]]], dim=0) if len(edge_list) else edge_list#获得逆向边
    edge_feats = torch.cat([edge_feats] * 2, dim=0) if len(edge_feats) else edge_feats#特征与边对应

    features = [(atom.GetIdx(), atom_features(atom, atom_symbols)) for atom in mol_graph.GetAtoms()]#
    features.sort()
    _, features = zip(*features)
    features = torch.stack(features)
    node_feature = features.long()#
    edge_feature = edge_feats.long()

    g = dgl.DGLGraph()#创建了一个空的图对象 g，该对象可以用来存储和表示图结构
    g.add_nodes(features.shape[0])#获得这个smiles分子有多少个原子
    g.ndata['feat'] = node_feature
    for src, dst in edge_list:
        g.add_edges(src.item(), dst.item())
    g.edata['feat'] = edge_feature
    data_dgl = g
    return data_dgl

def load_drug_mol_data(args):
    data = pd.read_csv(args.dataset_filename)
    drug_id_mol_tup = []#drugbank_id和RDKit分子图对象
    symbols = list()#单个原子符号列表
    drug_smile_dict = {}#drugbank_id和smiles字典
    smiles_rdkit_list = []#RDKit地址

    for id1, id2, smiles1, smiles2, relation in zip(data[args.c_id1], data[args.c_id2], data[args.c_s1],
                                                    data[args.c_s2], data[args.c_y]):
        drug_smile_dict[id1] = smiles1
        drug_smile_dict[id2] = smiles2

    for id, smiles in drug_smile_dict.items():
        #num = 0
        mol = Chem.MolFromSmiles(smiles.strip())#将SMILES转化成RDKit分子对象

        if mol is not None:
            drug_id_mol_tup.append((id, mol))
            symbols.extend(atom.GetSymbol() for atom in mol.GetAtoms())#GetAtoms()，获取分子所有元组对象，GetSymbol：获得原子的符号

    for m in drug_id_mol_tup:
        smiles_rdkit_list.append(m[-1])
    symbols = list(set(symbols))#列表转换成集合（消除重复）再将集合转换成列表
    ##返回每个原子特征（32，45）、边对（2，70）、有边相连矩阵中坐标（2，104）、边特征（70，6），相似矩阵（1，544）
    drug_data_pyg = {id: generate_drug_data(mol, symbols, smiles_rdkit_list,id,drug_smile_dict[id]) for id, mol in tqdm(drug_id_mol_tup, desc='Processing drugs_pyg')}
    #创建了一个图对象，并向其中添加了节点、边和节点特征。用于图数据的处理和分析
    drug_data_dgl = {id: generate_drug_data_dgl(mol, symbols) for id, mol in tqdm(drug_id_mol_tup, desc='Processing drugs_dgl')}
    save_data(drug_data_pyg, 'drug_data_pyg.pkl', args)
    save_data(drug_data_dgl, 'drug_data_dgl.pkl', args)
    return drug_data_pyg,drug_data_dgl


def generate_pair_triplets(args):
    pos_triplets = []

    with open(f'{args.dirname}/{args.dataset.lower()}/drug_data_pyg.pkl', 'rb') as f:
        f_loaded =  pickle.load(f)
        drug_ids = list(f_loaded.keys())
        # f_test=pickle.load(f)
        #test_smile=f_loaded[drug_ids[0]]
    data = pd.read_csv(args.dataset_filename)
    p_smile1_list = []
    p_smile2_list = []
    p_finger1_list = []
    p_finger2_list = []
    #读取fingger文件
    unique_drugbank_finger = pd.read_csv('Data/fingerprint.csv')
    unique_drugbank_finger['id'] = unique_drugbank_finger['id'].astype(str)
    for id1, id2, relation ,p_smile1,p_smile2 in zip(data[args.c_id1], data[args.c_id2], data[args.c_y],data[args.c_s1],data[args.c_s2]):
        if ((id1 not in drug_ids) or (id2 not in drug_ids)): continue
        # if args.dataset in ('drugbank','zhangddi'):
        # if args.dataset in ('drugbank'):
        #     relation -= 1
        pos_triplets.append([id1, id2, relation])
        p_smile1_list.append(p_smile1)
        p_smile2_list.append(p_smile2)
        row1 = unique_drugbank_finger[unique_drugbank_finger['id'] == id1]
        row2 = unique_drugbank_finger[unique_drugbank_finger['id'] == id2]
        if not row1.empty:
            finger1 = row1.iloc[:, 2:].values.tolist()
            # row['fingerprint'] = finger.values.tolist()
            p_finger1_list.append(finger1)
        else:
            p_finger1_list.append(None)  # 如果找不到对应的 ID，添加 None 到列表中
            print(id1 + str("have not finger"))
        if not row2.empty:
            finger2 = row2.iloc[:, 2:].values.tolist()
            # row['fingerprint'] = finger.values.tolist()
            p_finger2_list.append(finger2)
        else:
            p_finger2_list.append(None)  # 如果找不到对应的 ID，添加 None 到列表中
            print(id2 + str("have not finger"))
    if len(pos_triplets) == 0:
        raise ValueError('All tuples are invalid.')

    pos_triplets = np.array(pos_triplets)
    data_statistics = load_data_statistics(pos_triplets)
    drug_ids = np.array(drug_ids)

    neg_samples = []
    neg_smiles = []
    neg_finger = []
    for pos_item in tqdm(pos_triplets, desc='Generating Negative sample'):
        temp_neg = []
        h, t, r = pos_item[:3]

        if args.dataset == 'drugbank' or 'zhangddi':
        #if args.dataset == 'drugbank':
            neg_heads, neg_tails = _normal_batch(h, t, r, args.neg_ent, data_statistics, drug_ids, args)
            temp_neg = [str(neg_h) + '$h' for neg_h in neg_heads] + \
                       [str(neg_t) + '$t' for neg_t in neg_tails]
        else:
            existing_drug_ids = np.asarray(list(set(
                np.concatenate(
                    [data_statistics["ALL_TRUE_T_WITH_HR"][(h, r)], data_statistics["ALL_TRUE_H_WITH_TR"][(h, r)]],
                    axis=0)
            )))
            temp_neg = _corrupt_ent(existing_drug_ids, args.neg_ent, drug_ids, args)

        neg_samples.append('_'.join(map(str, temp_neg[:args.neg_ent])))

    neg_id = neg_samples
    for n_id in neg_id:
        # 去掉额外的 $t 部分
        n_id = n_id.split('$')[0]

        row = unique_drugbank_finger[unique_drugbank_finger['id'] == n_id]
        if not row.empty:
            smiles = row.iloc[0]['smiles']
            finger = row.iloc[:,2:].values.tolist()
            # row['fingerprint'] = finger.values.tolist()
            neg_smiles.append(smiles)
            neg_finger.append(finger)
        else:
            neg_smiles.append(None)  # 如果找不到对应的 ID，添加 None 到列表中
            print(n_id + str("have not smile"))
    df = pd.DataFrame({'Drug1_ID': pos_triplets[:, 0],
                       'Drug2_ID': pos_triplets[:, 1],
                       'p_smile1': p_smile1_list,
                       'p_smile2': p_smile2_list,
                       'Y': pos_triplets[:, 2],
                       'Neg samples': neg_samples,
                       'Neg_smiles':neg_smiles,
                       'p_finger1':p_finger1_list,
                       'p_finger2':p_finger2_list,
                       'Neg_finger':neg_finger})
    filename = f'{args.dirname}/{args.dataset}/pair_pos_neg_triplets.csv'
    df.to_csv(filename, index=False)
    print(f'\nData saved as {filename}!')
    save_data(data_statistics, 'data_statistics.pkl', args)


def load_data_statistics(all_tuples):
    print('Loading data statistics ...')
    statistics = dict()
    statistics["ALL_TRUE_H_WITH_TR"] = defaultdict(list)
    statistics["ALL_TRUE_T_WITH_HR"] = defaultdict(list)
    statistics["FREQ_REL"] = defaultdict(int)
    statistics["ALL_H_WITH_R"] = defaultdict(dict)
    statistics["ALL_T_WITH_R"] = defaultdict(dict)
    statistics["ALL_TAIL_PER_HEAD"] = {}
    statistics["ALL_HEAD_PER_TAIL"] = {}

    for h, t, r in tqdm(all_tuples, desc='Getting data statistics'):
        statistics["ALL_TRUE_H_WITH_TR"][(t, r)].append(h)
        statistics["ALL_TRUE_T_WITH_HR"][(h, r)].append(t)
        statistics["FREQ_REL"][r] += 1.0
        statistics["ALL_H_WITH_R"][r][h] = 1
        statistics["ALL_T_WITH_R"][r][t] = 1

    for t, r in statistics["ALL_TRUE_H_WITH_TR"]:
        statistics["ALL_TRUE_H_WITH_TR"][(t, r)] = np.array(list(set(statistics["ALL_TRUE_H_WITH_TR"][(t, r)])))
    for h, r in statistics["ALL_TRUE_T_WITH_HR"]:
        statistics["ALL_TRUE_T_WITH_HR"][(h, r)] = np.array(list(set(statistics["ALL_TRUE_T_WITH_HR"][(h, r)])))

    for r in statistics["FREQ_REL"]:
        statistics["ALL_H_WITH_R"][r] = np.array(list(statistics["ALL_H_WITH_R"][r].keys()))
        statistics["ALL_T_WITH_R"][r] = np.array(list(statistics["ALL_T_WITH_R"][r].keys()))
        statistics["ALL_HEAD_PER_TAIL"][r] = statistics["FREQ_REL"][r] / len(statistics["ALL_T_WITH_R"][r])
        statistics["ALL_TAIL_PER_HEAD"][r] = statistics["FREQ_REL"][r] / len(statistics["ALL_H_WITH_R"][r])

    print('getting data statistics done!')

    return statistics


def _corrupt_ent(positive_existing_ents, max_num, drug_ids, args):
    corrupted_ents = []
    while len(corrupted_ents) < max_num:
        candidates = args.random_num_gen.choice(drug_ids, (max_num - len(corrupted_ents)) * 2, replace=False)
        invalid_drug_ids = np.concatenate([positive_existing_ents, corrupted_ents], axis=0)
        mask = np.isin(candidates, invalid_drug_ids, assume_unique=True, invert=True)
        corrupted_ents.extend(candidates[mask])

    corrupted_ents = np.array(corrupted_ents)[:max_num]
    return corrupted_ents


def _normal_batch(h, t, r, neg_size, data_statistics, drug_ids, args):
    neg_size_h = 0
    neg_size_t = 0
    prob = data_statistics["ALL_TAIL_PER_HEAD"][r] / (data_statistics["ALL_TAIL_PER_HEAD"][r] +
                                                      data_statistics["ALL_HEAD_PER_TAIL"][r])
    # prob = 2
    for i in range(neg_size):
        if args.random_num_gen.random() < prob:
            neg_size_h += 1
        else:
            neg_size_t += 1

    return (_corrupt_ent(data_statistics["ALL_TRUE_H_WITH_TR"][t, r], neg_size_h, drug_ids, args),
            _corrupt_ent(data_statistics["ALL_TRUE_T_WITH_HR"][h, r], neg_size_t, drug_ids, args))


def save_data(data, filename, args):
    dirname = f'{args.dirname}/{args.dataset}'
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    filename = dirname + '/' + filename
    with open(filename, 'wb') as f:
        pickle.dump(data, f)
    print(f'\nData saved as {filename}!')


def split_data(args):
    filename = f'{args.dirname}/{args.dataset}/pair_pos_neg_triplets.csv'
    df = pd.read_csv(filename)
    seed = args.seed
    class_name = args.class_name
    save_to_filename = os.path.splitext(filename)[0]
    cv_split = StratifiedShuffleSplit(n_splits=3, test_size=0.2, random_state=seed)
    for fold_i, (train_index, test_index) in enumerate(cv_split.split(X=df, y=df[class_name])):
        print(f'Fold {fold_i} generated!')
        train_df = df.iloc[train_index]
        test_df = df.iloc[test_index]
        train_df.to_csv(f'{save_to_filename}_train_fold{fold_i}.csv', index=False)
        print(f'{save_to_filename}_train_fold{fold_i}.csv', 'saved!')
        test_df.to_csv(f'{save_to_filename}_test_fold{fold_i}.csv', index=False)
        print(f'{save_to_filename}_test_fold{fold_i}.csv', 'saved!')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--neg_ent', type=int, default=1, help='Number of negative samples')
    parser.add_argument('-s', '--seed', type=int, default=0, help='Seed for the random number generator')
    # parser.add_argument('-d', '--dataset', type=str, required=True, default='ZhangDDI',
    #                     choices=['drugbank', 'ZhangDDI'],help='Dataset to preprocess.')
    # parser.add_argument('-o', '--operation', type=str, required=True,default='all',
    #                     choices=['all', 'generate_triplets', 'drug_data', 'split'], help='Operation to perform')
    parser.add_argument('-t_r', '--test_ratio', type=float, default=0.2)
    parser.add_argument('-n_f', '--n_folds', type=int, default=3)
    parser.add_argument('-d', '--dataset', required=False, default='ZhangDDI',choices=['drugbank', 'ZhangDDI'],
                       help='Specify the dataset (drugbank or ZhangDDI)')
    parser.add_argument('-o', '--operation', required=False, default='all',choices=['all', 'generate_triplets', 'drug_data', 'split'],
                        help='Specify the operation (all, generate_triplets, drug_data, split)')
    dataset_columns_map = {
        'drugbank': ('d1', 'd2', 'smile1', 'smile2', 'type'),
        'twosides': ('Drug1_ID', 'Drug2_ID', 'Drug1', 'Drug2', 'New Y'),
        'zhangddi':('drugbank_id_1','drugbank_id_2','smiles_2','smiles_1','label')
    }

    dataset_file_name_map = {
        'ZhangDDI': ('/tmp/MCF-DDI/DrugBank/data/drugbank.tab', '\t')
    }
    args = parser.parse_args()
    args.dataset = args.dataset.lower()#将字符串转换成小写形式
    args.dataset = 'drugbank'
    # args.c_id1, args.c_id2, args.c_s2,args.c_s1, args.c_y = dataset_columns_map[args.dataset]
    args.c_id1, args.c_id2, args.c_s1, args.c_s2, args.c_y = dataset_columns_map[args.dataset]
    #args.dataset_filename, args.delimiter = dataset_file_name_map[args.dataset]
    args.dataset_filename = 'Data/drugbank.csv'
    args.dirname = 'Data'

    args.random_num_gen = np.random.RandomState(args.seed)
    #保存drug_data_pyg.pkl'：返回每个原子特征（32，45）、边对（2，70）、有边相连矩阵中坐标（2，104）、边特征（70，6），相似矩阵（1，544）
    if args.operation in ('all', 'drug_data'):
          load_drug_mol_data(args)

    if args.operation in ('all', 'generate_triplets'):
        generate_pair_triplets(args)

    if args.operation in ('all', 'split'):
        args.class_name = 'Y'
        split_data(args)
