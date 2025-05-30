# 2022.10.31-Changed for building ViG model
#            Huawei Technologies Co., Ltd. <foss@huawei.com>
import math
import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from torch.nn import Sequential as Seq
from gcn_lib import act_layer, DyGraphConv2d, get_2d_relative_pos_embed

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.helpers import load_pretrained
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.registry import register_model
from utils.checkpoint import get_missing_parameters_message, get_unexpected_parameters_message
from utils.logger import print_log
from .vig import Stem
from gcn_lib.torch_nn import batched_index_select
class Adapter(nn.Module):
    def __init__(self,
                 embed_dims,
                 reduction_dims,
                 drop_rate_adapter=0.1
                ):
        super(Adapter, self).__init__()
        self.embed_dims = embed_dims
        self.super_reductuion_dim = reduction_dims
        self.dropout = nn.Dropout(p=drop_rate_adapter)
        if self.super_reductuion_dim > 0:
            self.layer_norm = nn.LayerNorm(self.embed_dims)
            self.ln1 = nn.Linear(self.embed_dims, self.super_reductuion_dim)
            self.activate = nn.GELU()
            self.ln2 = nn.Linear(self.super_reductuion_dim, self.embed_dims)
            self.init_weights()
        
    def init_weights(self):
        def _init_weights(m):
            if isinstance(m, nn.Linear):
                nn.init.kaiming_uniform_(m.weight, mode='fan_in', nonlinearity='gelu')
                nn.init.normal_(m.bias, std=1e-6)
        self.apply(_init_weights)

    def set_sample_config(self, sample_embed_dim):
        self.sample_embed_dim = sample_embed_dim
        self.sampled_weight_0 = self.ln1.weight[:self.sample_embed_dim,:]
        self.sampled_bias_0 =  self.ln1.bias[:self.sample_embed_dim]
        self.sampled_weight_1 = self.ln2.weight[:, :self.sample_embed_dim]
        self.sampled_bias_1 =  self.ln2.bias

    def forward(self, x):
        x = self.layer_norm(x)
        scale = 0.7
        out = self.ln1(x)
        out = self.activate(out)
        out = self.dropout(out)
        out = self.ln2(out)
        return out*scale

class LoRPrompterDown(nn.Module):
    def __init__(self,
                 embed_dims,
                 rank_dims,
                 linear_transfrom=True
                ):
        super(LoRPrompterDown, self).__init__()
        self.embed_dims = embed_dims
        self.rank_dims = rank_dims
        self.linear_transfrom = linear_transfrom
        if not linear_transfrom:
            self.activate = nn.GELU()
        self.project_down = nn.Conv2d(self.embed_dims, self.rank_dims, kernel_size=1, bias=True)
        self.init_weights()
        
    def init_weights(self):
        def _init_weights(m):
            if isinstance(m, nn.Linear):
                nn.init.kaiming_uniform_(m.weight, mode='fan_in', nonlinearity='gelu')
                nn.init.normal_(m.bias, std=1e-6)
        self.apply(_init_weights)

    def forward(self, x):
        y = self.project_down(x) #[B, d, H, W] -> [B, r, H, W]
        if not self.linear_transfrom:
            y=self.activate(y)
        return y
    
class LoRPrompterUp(nn.Module):
    def __init__(self,
                 embed_dims,
                 rank_dims,
                 linear_transfrom=True
                ):
        super(LoRPrompterUp, self).__init__()
        self.embed_dims = embed_dims
        self.rank_dims = rank_dims
        self.linear_transfrom = linear_transfrom
        if not linear_transfrom:
            self.activate = nn.GELU()
        self.project_up = nn.Conv2d(self.rank_dims, self.embed_dims, kernel_size=1, bias=True)
        self.init_weights()
        
    def init_weights(self):
        def _init_weights(m):
            if isinstance(m, nn.Linear):
                nn.init.kaiming_uniform_(m.weight, mode='fan_in', nonlinearity='gelu')
                nn.init.normal_(m.bias, std=1e-6)
        self.apply(_init_weights)

    def forward(self, x):
        y = self.project_up(x) #[B, d, H, W] -> [B, r, H, W]
        if not self.linear_transfrom:
            y=self.activate(y)
        return y

class Grapher(nn.Module):
    """
    Grapher module with graph convolution and fc layers
    """
    def __init__(self, in_channels, kernel_size=9, dilation=1, conv='edge', act='relu', norm=nn.LayerNorm,
                 bias=True,  stochastic=False, epsilon=0.0, r=1, n=196, drop_path=0.0, relative_pos=False, prompt_length=14, rank_dim=32):
        super(Grapher, self).__init__()
        self.channels = in_channels
        self.n = n
        self.r = r
        self.fc1 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1, stride=1, padding=0),
            nn.BatchNorm2d(in_channels),
        )
        self.graph_conv = DyGraphConv2d(in_channels, in_channels * 2, kernel_size, dilation, conv, act, norm, bias, stochastic, epsilon, r)
        self.fc2 = nn.Sequential(
            nn.Conv2d(in_channels * 2, in_channels, 1, stride=1, padding=0),
            nn.BatchNorm2d(in_channels),
        )
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.relative_pos = None
        if relative_pos:
            print('using relative_pos')
            relative_pos_tensor = torch.from_numpy(np.float32(get_2d_relative_pos_embed(in_channels,int(n**0.5)))).unsqueeze(0).unsqueeze(1)
            relative_pos_tensor = F.interpolate(relative_pos_tensor, size=(n, n//(r*r)), mode='bicubic', align_corners=False)
            self.relative_pos = nn.Parameter(-relative_pos_tensor.squeeze(1), requires_grad=False)
        
        self.rank_dim = rank_dim
        self.node_prompts = nn.Parameter(torch.zeros([in_channels, prompt_length]))
        nn.init.kaiming_normal_(self.node_prompts)
        # self.low_rank_edge_prompts = nn.Parameter(torch.zeros([rank_dim, prompt_length, kernel_size]))
        # nn.init.kaiming_normal_(self.low_rank_edge_prompts)
        # self.edge_prompter = nn.Sequential(nn.Conv2d(rank_dim, in_channels, kernel_size=1))
        # self.edge_prompter = nn.Sequential(nn.Conv2d(rank_dim, in_channels, kernel_size=1), nn.GELU())
        self.graph_prompt = nn.Parameter(torch.zeros([rank_dim, in_channels]))
        # self.graph_prompt2 = nn.Parameter(torch.zeros([rank_dim, in_channels]))
        self.node_prompter = LoRPrompterDown(in_channels, rank_dims=rank_dim, linear_transfrom=False)
        # self.group_prompt = nn.ModuleList([LoRPrompterUp(embed_dims=in_channels, rank_dims=rank_dim, linear_transfrom=True) for i in range(kernel_size)])
        self.edge_prompt = LoRPrompterUp(embed_dims=in_channels, rank_dims=rank_dim, linear_transfrom=True)
        

    def _get_relative_pos(self, relative_pos, H, W):
        if relative_pos is None or H * W == self.n:
            return relative_pos
        else:
            N = H * W
            N_reduced = N // (self.r * self.r)
            return F.interpolate(relative_pos.unsqueeze(0), size=(N, N_reduced), mode="bicubic").squeeze(0)

    def forward(self, x):
        _tmp = x
        x = self.fc1(x)
        B, C, H, W = x.shape

        # low_rank_x = self.node_prompter(x) #[B, r, H, W]
        # res_node_prompts = low_rank_x.permute(0,2,3,1)@self.graph_prompt
        # res_node_prompts = res_node_prompts.permute(0,3,1,2)
        # x = x*0.8+res_node_prompts*0.2

        node_prompts = self.node_prompts[None,:,None,:]
        node_prompts = node_prompts.expand([B,-1,-1,-1])
        x = torch.cat([x, node_prompts], dim=-2)

        low_rank_x = self.node_prompter(x) #[B, r, H, W]
        res_node_prompts = low_rank_x.permute(0,2,3,1)@self.graph_prompt
        res_node_prompts = res_node_prompts.permute(0,3,1,2)
        x = x*0.8+res_node_prompts*0.2

        x, (neighbor_index, center_index) = self.graph_conv(x, require_edge=True)
        x = self.fc2(x)

        # res_node_prompts2 = low_rank_x.permute(0,2,3,1)@self.graph_prompt2
        # res_node_prompts2 = res_node_prompts2.permute(0,3,1,2)
        # x = x*0.8+res_node_prompts2*0.2

        # Neighbors low rank features propagate to Centers
        low_rank_neighbors = batched_index_select(low_rank_x.reshape(B, self.rank_dim, -1, 1), neighbor_index)
        # edge_prompts = []
        # for i in range(len(self.group_prompt)):
        #     edge_prompts.append(self.group_prompt[i](low_rank_neighbors[...,i:i+1]))
        # edge_prompts = torch.concat(edge_prompts, dim=-1)
        
        edge_prompts = self.edge_prompt(low_rank_neighbors)
        
        edge_prompts = torch.mean(edge_prompts, dim=-1, keepdim=False)
        edge_prompts = edge_prompts.reshape(B, C, -1, W)
        x = x*0.8 + edge_prompts*0.2

        # x = x + self.adapter(x.transpose(1,3)).transpose(1,3)

        # prompt_neighbor_index = neighbor_index[:,H*W:].unsqueeze(1).expand(-1,C,-1,-1).reshape(B,C,-1,1)
        # edge_prompts = self.edge_prompter(self.low_rank_edge_prompts).expand(B,-1,-1,-1).reshape(B,C,-1,1)*0.2
        
        # x = x.reshape(B, C, -1, 1).contiguous()
        # x = torch.scatter(x, -2, prompt_neighbor_index, edge_prompts)
        x = x[:,:,:H,:]
        x = self.drop_path(x) + _tmp
        return x


class FFN(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act='relu', drop_path=0.0, rank_dim=16):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Sequential(
            nn.Conv2d(in_features, hidden_features, 1, stride=1, padding=0),
            nn.BatchNorm2d(hidden_features),
        )
        self.act = act_layer(act)
        self.fc2 = nn.Sequential(
            nn.Conv2d(hidden_features, out_features, 1, stride=1, padding=0),
            nn.BatchNorm2d(out_features),
        )
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.graph_prompt = nn.Parameter(torch.zeros([rank_dim, in_features]))
        self.node_prompter = LoRPrompterDown(in_features, rank_dims=rank_dim, linear_transfrom=False)        

    def forward(self, x):
        shortcut = x
        x = self.fc1(x)
        x = self.act(x)
        x = self.fc2(x)
        
        low_rank_x = self.node_prompter(x) #[B, r, H, W]
        res_node_prompts = low_rank_x.permute(0,2,3,1)@self.graph_prompt
        res_node_prompts = res_node_prompts.permute(0,3,1,2)
        x = x*0.8+res_node_prompts*0.2

        x = self.drop_path(x) + shortcut
        return x

def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.proj', 'classifier': 'head',
        **kwargs
    }


default_cfgs = {
    'gnn_patch16_224': _cfg(
        crop_pct=0.9, input_size=(3, 224, 224),
        mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5),
    ),
}

class DeepGCN(torch.nn.Module):
    def __init__(self, opt):
        super(DeepGCN, self).__init__()
        channels = opt.n_filters
        k = opt.k
        act = opt.act
        norm = opt.norm
        bias = opt.bias
        epsilon = opt.epsilon
        stochastic = opt.use_stochastic
        conv = opt.conv
        self.n_blocks = opt.n_blocks
        drop_path = opt.drop_path
        
        self.stem = Stem(out_dim=channels, act=act)

        dpr = [round(x.item(), 3) for x in torch.linspace(0, drop_path, self.n_blocks)]  # stochastic depth decay rule 
        print('dpr', dpr)
        num_knn = [int(x.item()) for x in torch.linspace(k, 2*k, self.n_blocks)]  # number of knn's k
        print('num_knn', num_knn)
        max_dilation = 196 // max(num_knn)
        
        self.pos_embed = nn.Parameter(torch.zeros(1, channels, 14, 14))

        if opt.use_dilation:
            self.backbone = Seq(*[Seq(
                                      Grapher(channels, num_knn[i], min(i // 4 + 1, max_dilation), conv, act, norm, bias, stochastic, epsilon, 1, drop_path=dpr[i], prompt_length=14),
                                      FFN(channels, channels * 4, act=act, drop_path=dpr[i])
                                     ) for i in range(self.n_blocks)])
        else:
            self.backbone = Seq(*[Seq(
                                      Grapher(channels, num_knn[i], 1, conv, act, norm, bias, stochastic, epsilon, 1, drop_path=dpr[i], prompt_length=14),
                                      FFN(channels, channels * 4, act=act, drop_path=dpr[i])
                                     ) for i in range(self.n_blocks)])
        
        self.downstream_head = Seq(nn.Conv2d(channels, 1024, 1, bias=True),
                                   nn.BatchNorm2d(1024),
                                   act_layer(act),
                                   nn.Dropout(opt.dropout),
                                   nn.Conv2d(1024, opt.n_classes, 1, bias=True)
                                   )
        self.model_init()

    def model_init(self):
        for m in self.modules():
            if isinstance(m, torch.nn.Conv2d):
                torch.nn.init.kaiming_normal_(m.weight)
                m.weight.requires_grad = True
                if m.bias is not None:
                    m.bias.data.zero_()
                    m.bias.requires_grad = True

    def forward(self, inputs):
        x = self.stem(inputs)
        x = x + self.pos_embed
        B, C, H, W = x.shape
        
        for i in range(self.n_blocks):
            x = self.backbone[i][0](x)
            x = self.backbone[i][1](x)
        # max_node = F.adaptive_max_pool2d(x, 1)
        x = F.adaptive_avg_pool2d(x, 1)
        x = self.downstream_head(x)
        x = x.squeeze(-1).squeeze(-1)
        return x

    def load_model_from_ckpt(self, ckpt_path, logger=None):
        ckpt = torch.load(ckpt_path)
        incompatible = self.load_state_dict(ckpt, strict=False)
        if incompatible.missing_keys:
            print_log('missing_keys', logger=logger)
            print_log(get_missing_parameters_message(incompatible.missing_keys), logger=logger)
        if incompatible.unexpected_keys:
            print_log('unexpected_keys', logger=logger)
            print_log(get_unexpected_parameters_message(incompatible.unexpected_keys), logger=logger)

        print(f'[Transformer] Successful Loading the ckpt from {ckpt_path}')

@register_model
def vig_lor_gp_ti_224_gelu(**kwargs):
    class OptInit:
        def __init__(self, num_classes=1000, drop_path_rate=0.0, drop_rate=0.0, num_knn=9, **kwargs):
            self.k = num_knn # neighbor num (default:9)
            self.conv = 'mr' # graph conv layer {edge, mr}
            self.act = 'gelu' # activation layer {relu, prelu, leakyrelu, gelu, hswish}
            self.norm = 'batch' # batch or instance normalization {batch, instance}
            self.bias = True # bias of conv layer True or False
            self.n_blocks = 12 # number of basic blocks in the backbone
            self.n_filters = 192 # number of channels of deep features
            self.n_classes = num_classes # Dimension of out_channels
            self.dropout = drop_rate # dropout rate
            self.use_dilation = True # use dilated knn or not
            self.epsilon = 0.2 # stochastic epsilon for gcn
            self.use_stochastic = False # stochastic for gcn, True or False
            self.drop_path = drop_path_rate

    opt = OptInit(**kwargs)
    model = DeepGCN(opt)
    model.default_cfg = default_cfgs['gnn_patch16_224']
    return model


@register_model
def vig_lor_gp_s_224_gelu(**kwargs):
    class OptInit:
        def __init__(self, num_classes=1000, drop_path_rate=0.0, drop_rate=0.0, num_knn=9, **kwargs):
            self.k = num_knn # neighbor num (default:9)
            self.conv = 'mr' # graph conv layer {edge, mr}
            self.act = 'gelu' # activation layer {relu, prelu, leakyrelu, gelu, hswish}
            self.norm = 'batch' # batch or instance normalization {batch, instance}
            self.bias = True # bias of conv layer True or False
            self.n_blocks = 16 # number of basic blocks in the backbone
            self.n_filters = 320 # number of channels of deep features
            self.n_classes = num_classes # Dimension of out_channels
            self.dropout = drop_rate # dropout rate
            self.use_dilation = True # use dilated knn or not
            self.epsilon = 0.2 # stochastic epsilon for gcn
            self.use_stochastic = False # stochastic for gcn, True or False
            self.drop_path = drop_path_rate

    opt = OptInit(**kwargs)
    model = DeepGCN(opt)
    model.default_cfg = default_cfgs['gnn_patch16_224']
    return model


@register_model
def vig_lor_gp_b_224_gelu(**kwargs):
    class OptInit:
        def __init__(self, num_classes=1000, drop_path_rate=0.0, drop_rate=0.0, num_knn=9, **kwargs):
            self.k = num_knn # neighbor num (default:9)
            self.conv = 'mr' # graph conv layer {edge, mr}
            self.act = 'gelu' # activation layer {relu, prelu, leakyrelu, gelu, hswish}
            self.norm = 'batch' # batch or instance normalization {batch, instance}
            self.bias = True # bias of conv layer True or False
            self.n_blocks = 16 # number of basic blocks in the backbone
            self.n_filters = 640 # number of channels of deep features
            self.n_classes = num_classes # Dimension of out_channels
            self.dropout = drop_rate # dropout rate
            self.use_dilation = True # use dilated knn or not
            self.epsilon = 0.2 # stochastic epsilon for gcn
            self.use_stochastic = False # stochastic for gcn, True or False
            self.drop_path = drop_path_rate

    opt = OptInit(**kwargs)
    model = DeepGCN(opt)
    model.default_cfg = default_cfgs['gnn_patch16_224']
    return model
