'''
Heterogeneous NN reconstruction with BNB optimization of orientation
'''
import numpy as np
import sys, os
import argparse
import pickle
from datetime import datetime as dt

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

sys.path.insert(0,os.path.abspath(os.path.dirname(__file__))+'/lib-python')
from cryodrgn import mrc
from cryodrgn import utils
from cryodrgn import fft
from cryodrgn import dataset
from cryodrgn import lie_tools

from cryodrgn.lattice import Lattice
from cryodrgn.pose_search import PoseSearch
from cryodrgn.models import HetOnlyVAE
from cryodrgn.beta_schedule import get_beta_schedule, LinearSchedule
from cryodrgn.losses import EquivarianceLoss

try:
    import apex.amp as amp
except ImportError:
    pass

log = utils.log
vlog = utils.vlog 

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('particles', help='Input particles (.mrcs, .txt or .star)')
    parser.add_argument('-o', '--outdir', type=os.path.abspath, required=True, help='Output directory to save model')
    parser.add_argument('--load', type=os.path.abspath, help='Initialize training from a checkpoint')
    parser.add_argument('--load-poses', type=os.path.abspath, help='Initialize training from a checkpoint')
    parser.add_argument('--checkpoint', type=int, default=1, help='Checkpointing interval in N_EPOCHS (default: %(default)s)')
    parser.add_argument('--log-interval', type=int, default=1000, help='Logging interval in N_IMGS (default: %(default)s)')
    parser.add_argument('-v','--verbose',action='store_true',help='Increaes verbosity')
    parser.add_argument('--seed', type=int, default=np.random.randint(0,100000), help='Random seed')
    parser.add_argument('--invert-data', action='store_true', help='Invert data sign')
    parser.add_argument('--window', action='store_true', help='Real space windowing of dataset')
    parser.add_argument('--ind', type=os.path.abspath, help='Filter indices')

    group = parser.add_argument_group('Tilt series')
    group.add_argument('--tilt', help='Particle stack file (.mrcs)')
    group.add_argument('--tilt-deg', type=float, default=45, help='X-axis tilt offset in degrees (default: %(default)s)')
    group.add_argument('--enc-only', action='store_true', help='Use the tilt pair only in VAE and not in BNB search')
    
    group = parser.add_argument_group('Training parameters')
    group.add_argument('-n', '--num-epochs', type=int, default=10, help='Number of training epochs (default: %(default)s)')
    group.add_argument('-b','--batch-size', type=int, default=10, help='Minibatch size (default: %(default)s)')
    group.add_argument('--wd', type=float, default=0, help='Weight decay in Adam optimizer (default: %(default)s)')
    group.add_argument('--lr', type=float, default=1e-4, help='Learning rate in Adam optimizer (default: %(default)s)')
    group.add_argument('--beta', default=1.0, help='Choice of beta schedule or a constant for KLD weight (default: %(default)s)')
    group.add_argument('--beta-control', type=float, help='KL-Controlled VAE gamma. Beta is KL target. (default: %(default)s)')
    group.add_argument('--equivariance', type=float, help='Strength of equivariance loss (default: %(default)s)')
    group.add_argument('--eq-start-it', type=int, default=100000, help='It at which equivariance turned on (default: %(default)s)')
    group.add_argument('--eq-end-it', type=int, default=200000, help='It at which equivariance max (default: %(default)s)')
    group.add_argument('--norm', type=float, nargs=2, default=None, help='Data normalization as shift, 1/scale (default: mean, std of dataset)')
    group.add_argument('--l-ramp-epochs',type=int,default=0, help='default: %(default)s')
    group.add_argument('--l-ramp-model', type=int, default=0, help="If 1, then during ramp only train the model up to l-max")
    group.add_argument('--reset-model-every', type=int, help="If set, reset the model every N epochs")
    group.add_argument('--reset-optim-every', type=int, help="If set, reset the optimizer every N epochs")
    group.add_argument('--reset-optim-after-pretrain', type=int, help="If set, reset the optimizer every N epochs")

    group = parser.add_argument_group('Pose Search parameters')
    group.add_argument('--l-start', type=int,default=12, help='Starting L radius (default: %(default)s)')
    group.add_argument('--l-end', type=int, default=20, help='End L radius (default: %(default)s)')
    group.add_argument('--niter', type=int, default=5, help='Number of iterations of grid subdivision')
    group.add_argument('--t-extent', type=float, default=5, help='+/- pixels to search over translations (default: %(default)s)')
    group.add_argument('--t-ngrid', type=float, default=7, help='Initial grid size for translations')
    group.add_argument('--t-xshift', type=float, default=0)
    group.add_argument('--t-yshift', type=float, default=0)
    group.add_argument('--pretrain', type=int, default=10000, help='Number of initial iterations with random poses (default: %(default)s)')
    group.add_argument('--ps-freq', type=int, default=1, help='Frequency of pose inference (default: every %(default)s epochs)')
    group.add_argument('--nkeptposes', type=int, default=24, help="Number of poses to keep at each refinement interation during branch and bound")
    group.add_argument('--base-healpy', type=int, default=1, help="Base healpy grid for pose search. Higher means exponentially higher resolution.")
    group.add_argument('--half-precision', type=int, default=0, help="If 1, use half-precision for pose search")
    group.add_argument("--pose-model-update-freq", type=int, help="If set, only update the model used for pose search every N examples.")

    group = parser.add_argument_group('Encoder Network')
    group.add_argument('--qlayers', type=int, default=10, help='Number of hidden layers (default: %(default)s)')
    group.add_argument('--qdim', type=int, default=128, help='Number of nodes in hidden layers (default: %(default)s)')
    group.add_argument('--encode-mode', default='resid', choices=('conv','resid','mlp','tilt'), help='Type of encoder network (default: %(default)s)')
    group.add_argument('--zdim', type=int, default=1, help='Dimension of latent variable')
    group.add_argument('--enc-mask', type=int, help='Circular mask of image for encoder (default: D/2; -1 for no mask)')

    group = parser.add_argument_group('Decoder Network')
    group.add_argument('--players', type=int, default=10, help='Number of hidden layers (default: %(default)s)')
    group.add_argument('--pdim', type=int, default=128, help='Number of nodes in hidden layers (default: %(default)s)')
    group.add_argument('--pe-type', choices=('geom_ft','geom_full','geom_lowf','geom_nohighf','linear_lowf','none'), default='geom_lowf', help='Type of positional encoding')
    group.add_argument('--domain', choices=('hartley','fourier'), default='hartley')
    return parser

def make_model(args, lattice, enc_mask, in_dim):
    return HetOnlyVAE(
        lattice,
        args.qlayers,
        args.qdim,
        args.players,
        args.pdim,
        in_dim,
        args.zdim,
        encode_mode=args.encode_mode,
        enc_mask=enc_mask,
        enc_type=args.pe_type,
        domain=args.domain,
    )

def pretrain(model, lattice, optim, minibatch, tilt):
    y, yt = minibatch
    use_tilt = yt is not None
    B = y.size(0)

    model.train()
    optim.zero_grad()

    rot = lie_tools.random_SO3(B, device=y.device)
    z = torch.randn((B,model.zdim), device=y.device)

    # reconstruct circle of pixels instead of whole image
    mask = lattice.get_circular_mask(lattice.D//2)
    def gen_slice(R):
        return model.decode(lattice.coords[mask] @ R, z).view(B,-1)
    
    y = y.view(B,-1)[:, mask]
    if use_tilt:
        yt = yt.view(B,-1)[:, mask]
        gen_loss = .5*F.mse_loss(gen_slice(rot), y) + .5*F.mse_loss(gen_slice(tilt @ rot), yt)
    else:
        gen_loss = F.mse_loss(gen_slice(rot), y)
    
    # if args.half_precision:
    #     with amp.scale_loss(gen_loss, optim) as scaled_loss:
    #         scaled_loss.backward()
    # else:
    gen_loss.backward()
    optim.step()
    return gen_loss.item()

def train(model, lattice, ps, optim, L, minibatch, beta, beta_control=None, equivariance=None, enc_only=False, poses=None):
    y, yt = minibatch
    use_tilt = yt is not None
    B = y.size(0)

    # VAE inference of z
    model.train()
    optim.zero_grad()
    input_ = (y,yt) if use_tilt else (y,)
    z_mu, z_logvar = model.encode(*input_)
    z = model.reparameterize(z_mu, z_logvar)

    if equivariance is not None:
        lamb, equivariance_loss = equivariance
        eq_loss = equivariance_loss(y, z_mu)

    # pose inference
    if poses is not None: # use provided poses
        rot = poses[0]
        trans = poses[1]
    else: # pose search
        model.eval()
        with torch.no_grad():
            rot, trans, _base_pose = ps.opt_theta_trans(
                y,
                z=z,
                images_tilt=None if enc_only else yt,
            )
        model.train()

    # reconstruct circle of pixels instead of whole image
    mask = lattice.get_circular_mask(L)
    def gen_slice(R):
        return model.decode(lattice.coords[mask] @ R, z).view(B,-1)
    def translate(img):
        img = lattice.translate_ht(img, trans.unsqueeze(1), mask)
        return img.view(B,-1)

    y = y.view(B,-1)[:, mask]
    if use_tilt: yt = yt.view(B,-1)[:, mask]
    y = translate(y)
    if use_tilt: yt = translate(yt)    

    if use_tilt:
        gen_loss = .5*F.mse_loss(gen_slice(rot), y) + .5*F.mse_loss(gen_slice(bnb.tilt @ rot), yt)
    else:
        gen_loss = F.mse_loss(gen_slice(rot), y)

    kld = -0.5 * torch.mean(1 + z_logvar - z_mu.pow(2) - z_logvar.exp())
    if torch.isnan(kld):
        log(z_mu[0])
        log(z_logvar[0])
        raise RuntimeError('KLD is nan')

    if beta_control is None:
        loss = gen_loss + beta*kld/mask.sum()
    else:
        loss = gen_loss + args.beta_control*(beta-kld)**2/mask.sum()

    if equivariance is not None:
        loss += lamb*eq_loss

    # if args.half_precision:
    #     with amp.scale_loss(loss, optim) as scaled_loss:
    #         scaled_loss.backward()
    # else:
    loss.backward()

    optim.step()
    save_pose = [rot.detach().cpu().numpy()]
    save_pose.append(trans.detach().cpu().numpy())
    return gen_loss.item(), kld.item(), loss.item(), eq_loss.item() if equivariance else None, save_pose

def eval_z(model, lattice, data, batch_size, device, use_tilt=False):
    assert not model.training
    z_mu_all = []
    z_logvar_all = []
    data_generator = DataLoader(data, batch_size=batch_size, shuffle=False)
    for minibatch in data_generator:
        y = minibatch[0].to(device)
        if use_tilt: yt = minibatch[1].to(device)
        input_ = (y,yt) if use_tilt else (y,)
        z_mu, z_logvar = model.encode(*input_)
        z_mu_all.append(z_mu.detach().cpu().numpy())
        z_logvar_all.append(z_logvar.detach().cpu().numpy())
    z_mu_all = np.vstack(z_mu_all)
    z_logvar_all = np.vstack(z_logvar_all)
    return z_mu_all, z_logvar_all

def save_checkpoint(model, lattice, optim, epoch, norm, bnb_pose, z_mu, z_logvar, out_mrc_dir, out_weights, out_z, out_poses):
    '''Save model weights, latent encoding z, and decoder volumes'''
    # save model weights
    torch.save({
        'epoch':epoch,
        'model_state_dict':model.state_dict(),
        'optimizer_state_dict':optim.state_dict(),
        'bnb_pose': bnb_pose
        }, out_weights)
    # save z
    with open(out_z,'wb') as f:
        pickle.dump(z_mu, f)
        pickle.dump(z_logvar, f)
    with open(out_poses,'wb') as f:
        pickle.dump(bnb_pose, f)


def save_config(args, dataset, lattice, model, out_config):
    dataset_args = dict(particles=args.particles,
                        norm=dataset.norm,
                        invert_data=args.invert_data,
                        ind=args.ind,
                        #keepreal=args.use_real,
                        window=args.window)
    if args.tilt is not None:
        dataset_args['particles_tilt'] = args.tilt
    lattice_args = dict(D=lattice.D,
                        extent=lattice.extent,
                        ignore_DC=lattice.ignore_DC)
    model_args = dict(qlayers=args.qlayers,
                      qdim=args.qdim,
                      players=args.players,
                      pdim=args.pdim,
                      zdim=args.zdim,
                      encode_mode=args.encode_mode,
                      enc_mask=args.enc_mask,
                      pe_type=args.pe_type,
                      domain=args.domain)
    config = dict(dataset_args=dataset_args,
                  lattice_args=lattice_args,
                  model_args=model_args)
    config['seed'] = args.seed
    with open(out_config,'wb') as f:
        pickle.dump(config, f)

def sort_poses(poses):
    ind = [x[0] for x in poses]
    ind = np.concatenate(ind)
    rot = [x[1][0] for x in poses]
    rot = np.concatenate(rot)
    rot = rot[np.argsort(ind)]
    if len(poses[0][1]) == 2:
        trans = [x[1][1] for x in poses]
        trans = np.concatenate(trans)
        trans = trans[np.argsort(ind)]
        return (rot,trans)
    return (rot,)

def main(args):

    log(args)
    t1 = dt.now()
    if args.outdir is not None and not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # set the random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    ## set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    log('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    ## set beta schedule
    try:
        args.beta = float(args.beta)
    except ValueError: 
        assert args.beta_control, "Need to set beta control weight for schedule {}".format(args.beta)
    beta_schedule = get_beta_schedule(args.beta)

    # load the particles
    if args.ind is not None: 
        log('Filtering image dataset with {}'.format(args.ind))
        args.ind = pickle.load(open(args.ind,'rb'))
    if args.tilt is None:
        data = dataset.MRCData(args.particles, norm=args.norm, invert_data=args.invert_data, ind=args.ind, window=args.window)
        tilt = None
    else:
        assert args.encode_mode == 'tilt'
        data = dataset.TiltMRCData(args.particles, args.tilt, norm=args.norm, invert_data=args.invert_data, ind=args.ind, window=args.window)
        tilt = torch.tensor(utils.xrot(args.tilt_deg).astype(np.float32))
    Nimg = data.N
    D = data.D

    lattice = Lattice(D, extent=0.5)
    if args.enc_mask is None:
        args.enc_mask = D//2
    if args.enc_mask > 0:
        assert args.enc_mask <= D//2
        enc_mask = lattice.get_circular_mask(args.enc_mask)
        in_dim = enc_mask.sum()
    elif args.enc_mask == -1:
        enc_mask = None
        in_dim = D**2
    else:
        raise RuntimeError("Invalid argument for encoder mask radius {}".format(args.enc_mask))
    
    model = make_model(args, lattice, enc_mask, in_dim)
    log(model)
    log('{} parameters in model'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

    if args.pose_model_update_freq:
        pose_model = make_model(args, lattice, enc_mask, in_dim)
    else:
        pose_model = model
    
    # save configuration
    out_config = '{}/config.pkl'.format(args.outdir)
    save_config(args, data, lattice, model, out_config)

    ps = PoseSearch(model, lattice, args.l_start, args.l_end, tilt,
                    t_extent=args.t_extent, t_ngrid=args.t_ngrid, niter=args.niter,
                    nkeptposes=args.nkeptposes, base_healpy=args.base_healpy,
                    half_precision=args.half_precision, t_xshift=args.t_xshift, t_yshift=args.t_yshift)

    if args.equivariance:
        assert args.equivariance > 0, 'Regularization weight must be positive'
        equivariance_lambda = LinearSchedule(0, args.equivariance, args.eq_start_it, args.eq_end_it)
        equivariance_loss = EquivarianceLoss(model, D)

    optim = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

    if args.load:
        args.pretrain = 0
        log('Loading checkpoint from {}'.format(args.load))
        checkpoint = torch.load(args.load)
        model.load_state_dict(checkpoint['model_state_dict'])
        optim.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']+1
        model.train()
        if args.load_poses:
            sorted_poses = utils.load_pkl(args.load_poses)
    else:
        start_epoch = 0

    # if args.half_precision:
    #     model, optim = amp.initialize(model, optim, opt_level='O1')

    data_iterator = DataLoader(data, batch_size=args.batch_size, shuffle=True)

    # pretrain decoder with random poses
    global_it = 0
    pretrain_epoch = 0
    log('Using random poses for {} iterations'.format(args.pretrain))
    while global_it < args.pretrain:
        for batch in data_iterator:
            global_it += len(batch[0])
            batch = (batch[0].to(device), None) if tilt is None else (batch[0].to(device), batch[1].to(device))
            loss = pretrain(model, lattice, optim, batch, tilt=ps.tilt)
            if global_it % args.log_interval == 0:
                log(f'[Pretrain Iteration {global_it}] loss={loss:4f}')
            if global_it > args.pretrain:
                break

    # reset model after pretraining
    if optim.reset_optim_after_pretrain:
        log(">> Resetting optim after pretrain")
        optim = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

    # training loop
    num_epochs = args.num_epochs
    if args.pose_model_update_freq:
        pose_model.load_state_dict(model.state_dict())
        cc = 0
    for epoch in range(start_epoch, num_epochs):
        t2 = dt.now()
        kld_accum = 0
        gen_loss_accum = 0
        loss_accum = 0
        eq_loss_accum = 0
        batch_it = 0 
        poses, base_poses = [], []

        L_model = lattice.D // 2
        if args.l_ramp_epochs > 0:
            Lramp = args.l_start + int(epoch / args.l_ramp_epochs * (args.l_end - args.l_start))
            ps.Lmin = min(Lramp, args.l_start)
            ps.Lmax = min(Lramp, args.l_end)
            if epoch < args.l_ramp_epochs and args.l_ramp_model:
                L_model = ps.Lmax

        if args.reset_model_every and (epoch - 1) % args.reset_model_every == 0:
            log(">> Resetting model")
            model = make_model(args, lattice, enc_mask, in_dim)

        if args.reset_optim_every and (epoch - 1) % args.reset_optim_every == 0:
            log(">> Resetting optim")
            optim = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

        if epoch % args.ps_freq != 0:
            log('Using previous iteration poses')
        for batch in data_iterator:
            ind = batch[-1]
            ind_np = ind.cpu().numpy()
            batch = (batch[0].to(device), None) if tilt is None else (batch[0].to(device), batch[1].to(device))
            batch_it += len(batch[0])
            global_it = Nimg*epoch + batch_it

            beta = beta_schedule(global_it)
            if args.equivariance:
                lamb = equivariance_lambda(global_it)
                equivariance_tuple = (lamb, equivariance_loss)
            else: equivariance_tuple = None

            # train the model
            if epoch % args.ps_freq != 0:
                p = [torch.tensor(x[ind_np]) for x in sorted_poses]
            else: 
                p = None

            cc += len(batch[0])
            if args.pose_model_update_freq and cc > args.pose_model_update_freq:
                pose_model.load_state_dict(model.state_dict())
                cc = 0

            gen_loss, kld, loss, eq_loss, pose = train(model, lattice, ps, optim, L_model, batch, beta, args.beta_control, equivariance_tuple, enc_only=args.enc_only, poses=p)
            # logging
            poses.append((ind.cpu().numpy(),pose))
            kld_accum += kld*len(ind)
            gen_loss_accum += gen_loss*len(ind)
            if args.equivariance:eq_loss_accum += eq_loss*len(ind)

            loss_accum += loss*len(ind)
            if batch_it % args.log_interval == 0:
                eq_log = f'equivariance={eq_loss:.4f}, lambda={lamb:.4f}, ' if args.equivariance else ''
                log(f'# [Train Epoch: {epoch+1}/{num_epochs}] [{batch_it}/{Nimg} images] gen loss={gen_loss:.4f}, kld={kld:.4f}, beta={beta:.4f}, {eq_log}loss={loss:.4f}')

        eq_log = 'equivariance = {:.4f}, '.format(eq_loss_accum/Nimg) if args.equivariance else ''
        log('# =====> Epoch: {} Average gen loss = {:.4}, KLD = {:.4f}, {}total loss = {:.4f}; Finished in {}'.format(epoch+1, gen_loss_accum/Nimg, kld_accum/Nimg, eq_log, loss_accum/Nimg, dt.now() - t2))

        sorted_poses = sort_poses(poses) if poses else None

        # save checkpoint
        if args.checkpoint and epoch % args.checkpoint == 0:
            out_mrc = '{}/reconstruct.{}.mrc'.format(args.outdir,epoch)
            out_weights = '{}/weights.{}.pkl'.format(args.outdir,epoch)
            out_poses = '{}/pose.{}.pkl'.format(args.outdir, epoch)
            out_z = '{}/z.{}.pkl'.format(args.outdir, epoch)
            model.eval()
            with torch.no_grad():
                z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device, tilt is not None)
                save_checkpoint(model, lattice, optim, epoch, data.norm, sorted_poses, z_mu, z_logvar, out_mrc, out_weights, out_z, out_poses)

    ## save model weights and evaluate the model on 3D lattice
    model.eval()
    out_mrc = '{}/reconstruct'.format(args.outdir)
    out_weights = '{}/weights.pkl'.format(args.outdir)
    out_poses = '{}/pose.pkl'.format(args.outdir)
    out_z = '{}/z.pkl'.format(args.outdir)
    with torch.no_grad():
        z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device, tilt is not None)
        save_checkpoint(model, lattice, optim, epoch, data.norm, sorted_poses, z_mu, z_logvar, out_mrc, out_weights, out_z, out_poses)
    
    td = dt.now() - t1
    log('Finsihed in {} ({} per epoch)'.format(td, td/(num_epochs-start_epoch)))

if __name__ == '__main__':
    import cProfile
    pr = cProfile.Profile()
    pr.enable()


    args = parse_args().parse_args()
    utils._verbose = args.verbose
    main(args)

    pr.disable()
    pr.print_stats('cumtime')

