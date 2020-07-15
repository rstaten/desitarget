"""
desitarget.mtl
==============

Merged target lists.
"""

import numpy as np
import sys
from astropy.table import Table
import fitsio
from time import time

from desitarget.targetmask import obsmask, obsconditions
from desitarget.targets import calc_priority, calc_numobs_more
from desitarget.targets import main_cmx_or_sv, switch_main_cmx_or_sv
from desitarget.targets import set_obsconditions
from desitarget import io

# ADM the data model for MTL. Note that the _TARGET columns will have
# ADM to be changed on the fly for SV1_, SV2_, etc. files.
mtldatamodel = np.array([], dtype=[
    ('RA', '>f8'), ('DEC', '>f8'), ('PARALLAX', '>f4'),
    ('PMRA', '>f4'), ('PMDEC', '>f4'), ('REF_EPOCH', '>f4'),
    ('DESI_TARGET', '>i8'), ('BGS_TARGET', '>i8'), ('MWS_TARGET', '>i8'),
    ('SCND_TARGET', '>i8'), ('TARGETID', '>i8'),
    ('SUBPRIORITY', '>f8'), ('OBSCONDITIONS', '>i8'),
    ('PRIORITY_INIT', '>i8'), ('NUMOBS_INIT', '>i8'), ('PRIORITY', '>i8'),
    ('NUMOBS', '>i8'), ('NUMOBS_MORE', '>i8'), ('Z', '>f8'), ('ZWARN', '>i8'),
    ('TIMESTAMP', 'S19'), ('TARGET_STATE', 'S20')
    ])


def get_mtl_dir():
    """Convenience function to grab the MTL environment variable.

    Returns
    -------
    :class:`str`
        The directory stored in the $MTL_DIR environment variable.
    """
    # ADM check that the $MTL_DIR environment variable is set.
    mtldir = os.environ.get('MTL_DIR')
    if mtldir is None:
        msg = "Set $MTL_DIR environment variable!"
        log.critical(msg)
        raise ValueError(msg)

    return mtldir

def _get_mtl_nside():
    """Grab the HEALPixel nside to be used for MTL ledger files.

    Returns
    -------
    :class:`int`
        The HEALPixel nside number for MTL file creation and retrieval.
    """
    nside = 32

    return nside

def make_mtl(targets, obscon, zcat=None, trim=False, scnd=None):
    """Adds NUMOBS, PRIORITY, and OBSCONDITIONS columns to a targets table.

    Parameters
    ----------
    targets : :class:`~numpy.array` or `~astropy.table.Table`
        A numpy rec array or astropy Table with at least the columns
        ``TARGETID``, ``DESI_TARGET``, ``NUMOBS_INIT``, ``PRIORITY_INIT``.
        or the corresponding columns for SV or commissioning.
    obscon : :class:`str`
        A combination of strings that are in the desitarget bitmask yaml
        file (specifically in `desitarget.targetmask.obsconditions`), e.g.
        "DARK|GRAY". Governs the behavior of how priorities are set based
        on "obsconditions" in the desitarget bitmask yaml file.
    zcat : :class:`~astropy.table.Table`, optional
        Redshift catalog table with columns ``TARGETID``, ``NUMOBS``, ``Z``,
        ``ZWARN``.
    trim : :class:`bool`, optional
        If ``True`` (default), don't include targets that don't need
        any more observations.  If ``False``, include every input target.
    scnd : :class:`~numpy.array`, `~astropy.table.Table`, optional
        A set of secondary targets associated with the `targets`. As with
        the `target` must include at least ``TARGETID``, ``NUMOBS_INIT``,
        ``PRIORITY_INIT`` or the corresponding SV columns.
        The secondary targets will be padded to have the same columns
        as the targets, and concatenated with them.

    Returns
    -------
    :class:`~astropy.table.Table`
        MTL Table with targets columns plus:

        * NUMOBS_MORE    - number of additional observations requested
        * PRIORITY       - target priority (larger number = higher priority)
        * OBSCONDITIONS  - replaces old GRAYLAYER
    """
    start = time()
    # ADM set up the default logger.
    from desiutil.log import get_logger
    log = get_logger()

    # ADM determine whether the input targets are main survey, cmx or SV.
    colnames, masks, survey = main_cmx_or_sv(targets)
    # ADM set the first column to be the "desitarget" column
    desi_target, desi_mask = colnames[0], masks[0]

    # ADM if secondaries were passed, concatenate them with the targets.
    if scnd is not None:
        nrows = len(scnd)
        log.info('Pad {} primary targets with {} secondaries...t={:.1f}s'.format(
            len(targets), nrows, time()-start))
        padit = np.zeros(nrows, dtype=targets.dtype)
        sharedcols = set(targets.dtype.names).intersection(set(scnd.dtype.names))
        for col in sharedcols:
            padit[col] = scnd[col]
        targets = np.concatenate([targets, padit])
        # APC Propagate a flag on which targets came from scnd
        is_scnd = np.repeat(False, len(targets))
        is_scnd[-nrows:] = True
        log.info('Done with padding...t={:.1f}s'.format(time()-start))

    # Trim targets from zcat that aren't in original targets table
    if zcat is not None:
        ok = np.in1d(zcat['TARGETID'], targets['TARGETID'])
        num_extra = np.count_nonzero(~ok)
        if num_extra > 0:
            log.warning("Ignoring {} zcat entries that aren't "
                        "in the input target list".format(num_extra))
            zcat = zcat[ok]

    n = len(targets)
    # ADM if the input target columns were incorrectly called NUMOBS or PRIORITY
    # ADM rename them to NUMOBS_INIT or PRIORITY_INIT.
    # ADM Note that the syntax is slightly different for a Table.
    for name in ['NUMOBS', 'PRIORITY']:
        if isinstance(targets, Table):
            try:
                targets.rename_column(name, name+'_INIT')
            except KeyError:
                pass
        else:
            targets.dtype.names = [name+'_INIT' if col == name else col for col in targets.dtype.names]

    # ADM if a redshift catalog was passed, order it to match the input targets
    # ADM catalog on 'TARGETID'.
    if zcat is not None:
        # ADM there might be a quicker way to do this?
        # ADM set up a dictionary of the indexes of each target id.
        d = dict(tuple(zip(targets["TARGETID"], np.arange(n))))
        # ADM loop through the zcat and look-up the index in the dictionary.
        zmatcher = np.array([d[tid] for tid in zcat["TARGETID"]])
        ztargets = zcat
        if ztargets.masked:
            unobs = ztargets['NUMOBS'].mask
            ztargets['NUMOBS'][unobs] = 0
            unobsz = ztargets['Z'].mask
            ztargets['Z'][unobsz] = -1
            unobszw = ztargets['ZWARN'].mask
            ztargets['ZWARN'][unobszw] = -1
    else:
        ztargets = Table()
        ztargets['TARGETID'] = targets['TARGETID']
        ztargets['NUMOBS'] = np.zeros(n, dtype=np.int32)
        ztargets['Z'] = -1 * np.ones(n, dtype=np.float32)
        ztargets['ZWARN'] = -1 * np.ones(n, dtype=np.int32)
        # ADM if zcat wasn't passed, there is a one-to-one correspondence
        # ADM between the targets and the zcat.
        zmatcher = np.arange(n)

    # ADM extract just the targets that match the input zcat.
    targets_zmatcher = targets[zmatcher]

    # ADM update the number of observations for the targets.
    ztargets['NUMOBS_MORE'] = calc_numobs_more(targets_zmatcher, ztargets, obscon)

    # ADM assign priorities, note that only things in the zcat can have changed priorities.
    # ADM anything else will be assigned PRIORITY_INIT, below.
    priority = calc_priority(targets_zmatcher, ztargets, obscon)

    # If priority went to 0==DONOTOBSERVE or 1==OBS or 2==DONE, then NUMOBS_MORE should also be 0.
    # ## mtl['NUMOBS_MORE'] = ztargets['NUMOBS_MORE']
    ii = (priority <= 2)
    log.info('{:d} of {:d} targets have priority zero, setting N_obs=0.'.format(np.sum(ii), n))
    ztargets['NUMOBS_MORE'][ii] = 0

    # - Set the OBSCONDITIONS mask for each target bit.
    obsconmask = set_obsconditions(targets)

    # APC obsconmask will now be incorrect for secondary-only targets. Fix this
    # APC using the mask on secondary targets.
    if scnd is not None:
        obsconmask[is_scnd] = set_obsconditions(targets[is_scnd], scnd=True)

    # ADM set up the output mtl table.
    mtl = Table(targets)
    mtl.meta['EXTNAME'] = 'MTL'
    # ADM any target that wasn't matched to the ZCAT should retain its
    # ADM original (INIT) value of PRIORITY and NUMOBS.
    mtl['NUMOBS_MORE'] = mtl['NUMOBS_INIT']
    mtl['PRIORITY'] = mtl['PRIORITY_INIT']
    # ADM now populate the new mtl columns with the updated information.
    mtl['OBSCONDITIONS'] = obsconmask
    mtl['PRIORITY'][zmatcher] = priority
    mtl['NUMOBS_MORE'][zmatcher] = ztargets['NUMOBS_MORE']

    # Filter out any targets marked as done.
    if trim:
        notdone = mtl['NUMOBS_MORE'] > 0
        log.info('{:d} of {:d} targets are done, trimming these'.format(
            len(mtl) - np.sum(notdone), len(mtl))
        )
        mtl = mtl[notdone]

    # Filtering can reset the fill_value, which is just wrong wrong wrong
    # See https://github.com/astropy/astropy/issues/4707
    # and https://github.com/astropy/astropy/issues/4708
    mtl['NUMOBS_MORE'].fill_value = -1

    log.info('Done...t={:.1f}s'.format(time()-start))

    return mtl

def make_ledger_in_hp(hpdirname, nside, pixnum, obscon="DARK"):
    """
    Make an initial MTL ledger file in a single HEALPixel.

    Parameters
    ----------
    hpdirname : :class:`str`
        Full path to either a directory containing targets that
        have been partitioned by HEALPixel (i.e. as made by
        `select_targets` with the `bundle_files` option). Or the
        name of a single file of targets.
    nside : :class:`int`
        (NESTED) HEALPixel nside.
    pixnum : :class:`int`
        A single HEALPixel number.
    obscon : :class:`str`, optional, defaults to "DARK"
        A string matching ONE obscondition in the desitarget bitmask yaml
        file (i.e. in `desitarget.targetmask.obsconditions`), e.g. "GRAY"
        Governs how priorities are set based on "obsconditions". Also
        governs the sub-directory to which the ledger is written.
    """
    # ADM grab the datamodel for the targets.
    _, dt = io.read_targets_header(hpdirname, dtype=True)
    # ADM the MTL datamodel must reflect the target flavor (SV, etc.).
    mtldm = switch_main_cmx_or_sv(mtldatamodel, np.array([], dt))
    sharedcols = list(set(mtldm.dtype.names).intersection(dt.names))

    # ADM read in the needed columns from the targets.
    targs = io.read_targets_in_hp(hpdirname, nside, pixnum, columns=sharedcols)
        
    # ADM execute MTL.
    mtl = make_mtl(targs, obscon)

    # ADM add the timestamp for when MTL was run.
    mtl["TIMESTAMP"] = datetime.utcnow().isoformat(timespec='seconds')
    _, Mxs, _ = main_cmx_or_sv(targs)

    # ADM add the target state based on what PRIORITY was assigned.
    # ADM first set the null name and length for the TARGET_STATE column.
    nostate = np.empty(len(mtl), dtype=mtldm["TARGET_STATE"].dtype)
    nostate[:] = "NOSTATE"
    mtl["TARGET_STATE"] = nostate
    # ADM this just builds a dictonary of bitnames for each priority.
    bitnames, prios = [], []
    for Mx in Mxs:
        for bitname in Mx.names():
            try:
                prio = Mx[bitname].priorities["UNOBS"]
                if prio not in prios:
                    bitnames.append(bitname)
                    prios.append(Mx[bitname].priorities["UNOBS"])
            except:
                pass
    priodict = {key:val for key, val in zip(prios, bitnames)}
    # ADM anything with 0 priority should be for calibration.
    priodict[0] = "CALIBRATION"
    # ADM now loop through the priorities to add the state string.
    for prio in priodict:    
        ii = mtl["PRIORITY_INIT"] == prio
        mtl["TARGET_STATE"][ii] = priodict[prio]

    return
    
def make_ledger(hpdirname, obscon="DARK", numproc=1):
    """
    Make initial MTL ledger files for all HEALPixels.

    Parameters
    ----------
    hpdirname : :class:`str`
        Full path to either a directory containing targets that
        have been partitioned by HEALPixel (i.e. as made by
        `select_targets` with the `bundle_files` option). Or the
        name of a single file of targets.
    obscon : :class:`str`, optional, defaults to "DARK"
        A string matching ONE obscondition in the desitarget bitmask yaml
        file (i.e. in `desitarget.targetmask.obsconditions`), e.g. "GRAY"
        Governs how priorities are set based on "obsconditions". Also
        governs the sub-directory to which the ledger is written.
    numproc : :class:`int`, optional, defaults to 1 for serial
        Number of processes to parallelize across.
    """
    # ADM grab information apropos how the targets were constructed.
    hdr = io.read_targets_header(hpdirname)
    # ADM check the obscon for which the targets were made is
    # ADM consistent with the requested obscon.
    oc = hdr["OBSCON"]
    if obscon not in oc:
        msg = "File is type {} but requested behavior is {}".format(oc, obscon)
        log.critical(msg)
        raise ValueError(msg)

    # ADM optimal nside for reading in the targeting files.
    nside = hdr["FILENSID"]
    npixels = hp.nside2npix(nside)
    pixels = np.arange(npixels)
    
    # ADM the common function that is actually parallelized across.
    def _make_ledger_in_hp(pixnum):
        """make initial ledger in a single HEALPixel"""
        return make_ledger_in_hp(hpdirname, nside, pixnum, obscon=obscon)

    # ADM this is just to count pixels in _update_status.
    npix = np.zeros((), dtype='i8')
    t0 = time()

    def _update_status(result):
        """wrap key reduction operation on the main parallel process"""
        if npix % 2 == 0 and npix > 0:
            rate = (time() - t0) / npix
            log.info('{}/{} HEALPixels; {:.1f} secs/pixel...t = {:.1f} mins'.
                     format(npix, npixels, rate, (time()-t0)/60.))
        npix[...] += 1
        return result

    # ADM Parallel process across HEALPixels.
    if numproc > 1:
        pool = sharedmem.MapReduce(np=numproc)
        with pool:
            pool.map(_make_legder_in_hp, pixels, reduce=_update_status)
    else:
        for pixel in pixels:
            _update_status(_make_bright_star_mx(pixel))

    log.info("Done writing ledger...t = {:.1f} mins".format((time()-t0)/60.))

    return
