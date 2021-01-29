"""
TODO

    - make a tester for it
    - test it in nsim
    - make it possible to specify the guess type (not just psf)

"""
from pprint import pprint
import numpy as np
from numpy import where, array, sqrt, log, linspace, zeros
from numpy import isfinite
from numpy.linalg import LinAlgError

from . import admom
from . import fitting
from .gmix import GMix, GMixModel, GMixCM, get_coellip_npars
from . import em
from .observation import ObsList, MultiBandObsList, get_mb_obs
from .guessers import (
    TFluxGuesser,
    TFluxAndPriorGuesser,
    ParsGuesser,
)
from .gexceptions import GMixRangeError, BootPSFFailure, BootGalFailure

from . import metacal

from copy import deepcopy

BOOT_S2N_LOW = 2 ** 0
BOOT_R2_LOW = 2 ** 1
BOOT_R4_LOW = 2 ** 2
BOOT_TS2N_ROUND_FAIL = 2 ** 3
BOOT_ROUND_CONVOLVE_FAIL = 2 ** 4
BOOT_WEIGHTS_LOW = 2 ** 5


class Bootstrapper(object):
    def __init__(self, obs, verbose=False, **kw):
        """
        The data can be mutated: If a PSF fit is performed, the gmix will be
        set for the input PSF observation

        parameters
        ----------
        obs: observation(s)
            Either an Observation, ObsList, or MultiBandObsList The
            Observations must have a psf set.

            If the psf observations already have gmix objects set, there is no
            need to run fit_psfs()
        """

        self.verbose = verbose

        # this never gets modified in any way
        self.mb_obs_list_orig = get_mb_obs(obs)

        # this will get replaced if fit_psfs is run
        self.mb_obs_list = self.mb_obs_list_orig

        self.model_fits = {}

    def get_isampler(self):
        """
        get the importance sampler
        """
        if not hasattr(self, "isampler"):
            raise RuntimeError("you need to run isample() successfully first")
        return self.isampler

    def get_psampler(self):
        """
        get the prior samples sampler
        """
        if not hasattr(self, "psampler"):
            raise RuntimeError("you need to run psample() successfully first")
        return self.psampler

    def get_max_fitter(self):
        """
        get the maxlike fitter for the galaxy
        """
        if not hasattr(self, "max_fitter"):
            raise RuntimeError("you need to run fit_max successfully first")
        return self.max_fitter

    get_fitter = get_max_fitter

    def get_psf_flux_result(self):
        """
        get the result fromrunning fit_gal_psf_flux
        """
        if not hasattr(self, "psf_flux_res"):
            self.fit_gal_psf_flux()

        return self.psf_flux_res

    def fit_psfs(
        self,
        psf_model,
        Tguess,
        Tguess_key=None,
        skip_failed=True,
        ntry=4,
        fit_pars=None,
        skip_already_done=True,
        norm_key=None,
        min_s2n=None,
    ):
        """
        Fit all psfs.  If the psf observations already have a gmix
        then this step is not necessary

        parameters
        ----------
        psf_model: string
            The model to fit, e.g. 'em1','em2','em3','turb','gauss', etc.
        Tguess: float
            Guess for T
        skip_failed: bool
            If True, failures are just skipped when fitting the galaxy;
            in other words those observations will be ignored.  If False
            then an exception is raised
        ntry: integer
            Number of retries if the psf fit fails
        fit_pars: dict
            Fitting parameters for psf.
        skip_already_done: bool
            Skip psfs with a gmix already set
        norm_key:
            will use this key in the PSF meta data to fudge the
            normalization of the PSF model via
            amplitude -> amplitude*norm where amplitude is the PSF
            normalization (usually 1)
        """

        ntot = 0
        new_mb_obslist = MultiBandObsList()

        mb_obs_list = self.mb_obs_list
        for band, obslist in enumerate(mb_obs_list):
            new_obslist = ObsList()

            for i, obs in enumerate(obslist):
                if not obs.has_psf():
                    raise RuntimeError("observation does not have a psf set")

                try:

                    # this is a metacal thing
                    if hasattr(obs, "psf_nopix"):
                        if skip_already_done and obs.psf_nopix.has_gmix():
                            # print("skipping nopix psf fit")
                            # pass but don't continue, since we may still need
                            # to fit some images below
                            pass
                        else:
                            self._fit_one_psf(
                                obs.psf_nopix,
                                psf_model,
                                Tguess,
                                ntry,
                                fit_pars,
                            )

                    psf_obs = obs.get_psf()
                    if skip_already_done:
                        # if have a gmix, skip it
                        if psf_obs.has_gmix():
                            # print("skipping psf fit")
                            new_obslist.append(obs)
                            ntot += 1
                            continue

                        # if have a fitter and flags != 0, skip it
                        if "fitter" in psf_obs.meta:
                            tres = psf_obs.meta["fitter"].get_result()
                            if tres["flags"] != 0:
                                print(
                                    "    failed psf fit band %d obs %d, "
                                    "skipping observation" % (band, i)
                                )
                                continue

                    if Tguess_key is not None:
                        Tguess_i = psf_obs.meta[Tguess_key]
                    else:
                        Tguess_i = Tguess

                    self._fit_one_psf(
                        psf_obs,
                        psf_model,
                        Tguess_i,
                        ntry,
                        fit_pars,
                        norm_key=norm_key,
                    )

                    if min_s2n is not None:
                        res = psf_obs.meta["fitter"].get_result()
                        s2n = res["flux"] / res["flux_err"]
                        if s2n < min_s2n:
                            res["flags"] = BOOT_S2N_LOW
                            psf_obs.gmix = None
                            raise BootPSFFailure("    low psf s/n %g" % s2n)

                    new_obslist.append(obs)
                    ntot += 1

                except BootPSFFailure as err:
                    if not skip_failed:
                        raise
                    else:
                        print(
                            "    failed psf fit band %d obs %d: %s.  "
                            "skipping observation" % (band, i, str(err))
                        )
                        continue

            new_mb_obslist.append(new_obslist)

        if ntot == 0:
            raise BootPSFFailure("no psf fits succeeded")

        self.mb_obs_list = new_mb_obslist

    def _fit_one_psf(
        self, psf_obs, psf_model, Tguess, ntry, fit_pars, norm_key=None
    ):
        """
        fit the psf using a PSFRunner or PSFRunnerEM

        TODO: add bootstrapping T guess as well, from unweighted moments
        """

        if "em" in psf_model:
            runner = self._fit_one_psf_em(
                psf_obs, psf_model, Tguess, ntry, fit_pars
            )
        elif "coellip" in psf_model:
            runner = self._fit_one_psf_coellip(
                psf_obs, psf_model, Tguess, ntry, fit_pars
            )
        elif psf_model == "am":
            runner = self._fit_one_psf_am(psf_obs, Tguess, ntry)
        else:
            runner = self._fit_one_psf_max(
                psf_obs, psf_model, Tguess, ntry, fit_pars
            )

        psf_fitter = runner.fitter
        res = psf_fitter.get_result()
        psf_obs.update_meta_data({"fitter": psf_fitter})

        if res["flags"] == 0:
            self.psf_fitter = psf_fitter
            gmix = self.psf_fitter.get_gmix()

            if norm_key is not None:
                gmix.set_psum(psf_obs.meta[norm_key])

            psf_obs.set_gmix(gmix)

        else:
            raise BootPSFFailure("failed to fit psfs: %s" % str(res))

    def _fit_one_psf_em(self, psf_obs, psf_model, Tguess, ntry, fit_pars):

        ngauss = get_em_ngauss(psf_model)
        em_pars = {"tol": 1.0e-6, "maxiter": 50000}
        if fit_pars is not None:
            em_pars.update(fit_pars)

        runner = PSFRunnerEM(
            obs=psf_obs, Tguess=Tguess, ngauss=ngauss, em_pars=em_pars,
        )
        runner.go(ntry=ntry)

        return runner

    def _fit_one_psf_am(self, psf_obs, Tguess, ntry):
        runner = AMRunner(psf_obs, Tguess)
        runner.go(ntry=ntry)
        return runner

    def _fit_one_psf_coellip(self, psf_obs, psf_model, Tguess, ntry, fit_pars):

        ngauss = get_coellip_ngauss(psf_model)
        lm_pars = {
            "maxfev": 4000,
            "xtol": 5.0e-5,
            "ftol": 5.0e-5,
        }

        if fit_pars is not None:
            lm_pars.update(fit_pars)

        runner = PSFRunnerCoellip(psf_obs, Tguess, ngauss, lm_pars)
        runner.go(ntry=ntry)

        return runner

    def _fit_one_psf_max(self, psf_obs, psf_model, Tguess, ntry, fit_pars):
        lm_pars = {
            "maxfev": 4000,
            "xtol": 5.0e-5,
            "ftol": 5.0e-5,
        }

        if fit_pars is not None:
            lm_pars.update(fit_pars)

        runner = PSFRunner(psf_obs, psf_model, Tguess, lm_pars)
        runner.go(ntry=ntry)

        return runner

    def replace_masked_pixels(
        self, inplace=False, method="best-fit", fitter=None, add_noise=False
    ):
        """
        replaced masked pixels

        If a modification is made, the original image is stored for each
        Observation as .image_orig

        The original mb_obs_list is always in self.mb_obs_list_old,
        which is just a ref if inplace=True

        parameters
        ----------
        inplace: bool
            If True, modify the data in place.  Default False; a full
            copy is made.
        method: string, optional
            Method for replacement.  Supported methods are 'best-fit'.
            Default is 'best-fit'
        fitter: a fitter from fitters.py
            If not sent, the max fitter from self is used.

        add_noise: bool
            If True, add noise to the replaced pixels based on the median
            noise in the image, derived from the weight map
        """

        self.mb_obs_list_old = self.mb_obs_list

        if fitter is None:
            fitter = self.get_max_fitter()

        self.mb_obs_list = replace_masked_pixels(
            self.mb_obs_list,
            inplace=inplace,
            method=method,
            fitter=fitter,
            add_noise=add_noise,
        )

    def fit_max(
        self,
        gal_model,
        pars,
        guess=None,
        guess_widths=None,
        guesser=None,
        prior=None,
        extra_priors=None,
        ntry=1,
    ):
        """
        fit the galaxy.  You must run fit_psf() successfully first

        extra_priors is ignored here but used in composite
        """

        self.max_fitter = self._fit_one_model_max(
            gal_model,
            pars,
            guess=guess,
            guesser=guesser,
            prior=prior,
            ntry=ntry,
            guess_widths=guess_widths,
        )

    def fit_max_fixT(
        self,
        gal_model,
        pars,
        T,
        guess=None,
        prior=None,
        extra_priors=None,
        ntry=1,
    ):
        """
        fit the galaxy.  You must run fit_psf() successfully first

        extra_priors is ignored here but used in composite
        """

        if not hasattr(self, "psf_flux_res"):
            self.fit_gal_psf_flux()

        guesser = self._get_max_guesser(guess=guess, prior=prior)

        runner = MaxRunnerFixT(
            self.mb_obs_list, gal_model, pars, guesser, T, prior=prior
        )

        runner.go(ntry=ntry)

        fitter = runner.fitter

        res = fitter.get_result()

        if res["flags"] != 0:
            raise BootGalFailure("failed to fit galaxy with maxlike")

        self.max_fitter = fitter

    def fit_max_gonly(
        self,
        gal_model,
        max_pars,
        pars_in,
        guess=None,
        prior=None,
        extra_priors=None,
        ntry=1,
    ):
        """
        fit the galaxy.  You must run fit_psf() successfully first

        extra_priors is ignored here but used in composite
        """

        if not hasattr(self, "psf_flux_res"):
            self.fit_gal_psf_flux()

        if prior is not None:
            guesser = prior.sample
        else:
            guesser = self._get_max_guesser(guess=guess, prior=prior)

        runner = MaxRunnerGOnly(
            self.mb_obs_list,
            gal_model,
            max_pars,
            guesser,
            pars_in,
            prior=prior,
        )

        runner.go(ntry=ntry)

        fitter = runner.fitter

        res = fitter.get_result()

        if res["flags"] != 0:
            raise BootGalFailure("failed to fit galaxy with maxlike")

        self.max_fitter = fitter

    def _fit_one_model_max(
        self,
        gal_model,
        pars,
        guess=None,
        guess_widths=None,
        guesser=None,
        prior=None,
        ntry=1,
        obs=None,
    ):
        """
        fit the galaxy.  You must run fit_psf() successfully first
        """

        if obs is None:
            obs = self.mb_obs_list

        if not hasattr(self, "psf_flux_res"):
            self.fit_gal_psf_flux()

        if guesser is None:
            guesser = self._get_max_guesser(
                guess=guess, prior=prior, widths=guess_widths,
            )

        if gal_model == "bdf":
            runner = BDFRunner(obs, pars, guesser, prior=prior,)

        else:
            runner = MaxRunner(obs, gal_model, pars, guesser, prior=prior,)

        runner.go(ntry=ntry)

        fitter = runner.fitter

        res = fitter.get_result()

        if res["flags"] != 0:
            raise BootGalFailure("failed to fit galaxy with maxlike")

        return fitter

    def isample(self, ipars, prior=None):
        """
        bootstrap off the maxlike run
        """

        max_fitter = self.max_fitter
        use_fitter = max_fitter

        for i, nsample in enumerate(ipars["nsample"]):
            sampler = self._make_isampler(use_fitter, ipars)
            if sampler is None:
                raise BootGalFailure("isampling failed")

            sampler.make_samples(nsample)

            sampler.set_iweights(max_fitter.calc_lnprob)
            sampler.calc_result()

            tres = sampler.get_result()

            if self.verbose:
                print("    eff iter %d: %.2f" % (i, tres["efficiency"]))
            use_fitter = sampler

        maxres = max_fitter.get_result()
        tres["model"] = maxres["model"]

        self.isampler = sampler

    def _make_isampler(self, fitter, ipars):
        from .fitting import ISampler

        res = fitter.get_result()
        icov = res["pars_cov"]

        try:
            sampler = ISampler(
                res["pars"],
                icov,
                ipars["df"],
                min_err=ipars["min_err"],
                max_err=ipars["max_err"],
                ifactor=ipars.get("ifactor", 1.0),
                asinh_pars=ipars.get("asinh_pars", []),
                verbose=self.verbose,
            )

        except LinAlgError:
            print("        bad cov")
            sampler = None

        return sampler

    def psample(self, psample_pars, samples):
        """
        bootstrap off the maxlike run
        """
        from .fitting import PSampler, MaxSimple

        max_fitter = self.get_max_fitter()
        res = max_fitter.get_result()

        model = res["model"]
        tfitter = MaxSimple(self.mb_obs_list, model)
        tfitter._setup_data(res["pars"])

        sampler = PSampler(
            res["pars"],
            res["pars_err"],
            samples,
            verbose=self.verbose,
            **psample_pars
        )

        sampler.calc_loglikes(tfitter.calc_lnprob)

        self.psampler = sampler

        res = sampler.get_result()
        if res["flags"] != 0:
            raise BootGalFailure("psampling failed")

    def fit_gal_psf_flux(self, normalize_psf=True):
        """
        use psf as a template, measure flux (linear)
        """

        mbo = self.mb_obs_list
        nband = len(mbo)

        flags = []
        psf_flux = zeros(nband) - 9999.0
        psf_flux_err = zeros(nband)

        for i, obs_list in enumerate(mbo):

            if len(obs_list) == 0:
                raise BootPSFFailure("no epochs for band %d" % i)

            if not obs_list[0].has_psf_gmix():
                raise RuntimeError("you need to fit the psfs first")

            fitter = fitting.TemplateFluxFitter(
                obs_list, do_psf=True, normalize_psf=normalize_psf,
            )
            fitter.go()

            res = fitter.get_result()
            tflags = res["flags"]
            flags.append(tflags)

            if tflags == 0:

                psf_flux[i] = res["flux"]
                psf_flux_err[i] = res["flux_err"]

            else:
                print("failed to fit psf flux for band", i)

        self.psf_flux_res = {
            "flags": flags,
            "psf_flux": psf_flux,
            "psf_flux_err": psf_flux_err,
        }

    def _get_max_guesser(self, guess=None, prior=None, widths=None):
        """
        get a guesser that uses the psf T and galaxy psf flux to
        generate a guess, drawing from priors on the other parameters
        """

        scaling = "linear"

        if guess is not None:
            guesser = ParsGuesser(guess, scaling=scaling, widths=widths)
        else:
            psf_T = self.mb_obs_list[0][0].psf.gmix.get_T()

            pres = self.get_psf_flux_result()

            if prior is None:
                guesser = TFluxGuesser(
                    psf_T, pres["psf_flux"], scaling=scaling
                )
            else:
                guesser = TFluxAndPriorGuesser(
                    psf_T, pres["psf_flux"], prior, scaling=scaling
                )
        return guesser

    def try_replace_cov(self, cov_pars, fitter=None):
        """
        the lm cov often mis-estimates the error on the ellipticity parameters,
        try to replace it
        """
        if not hasattr(self, "max_fitter"):
            raise RuntimeError("you need to fit with the max like first")

        if fitter is None:
            fitter = self.max_fitter

        # reference to res
        res = fitter.get_result()

        fitter.calc_cov(cov_pars["h"], cov_pars["m"])

        if res["flags"] != 0:
            print("        cov replacement failed")
            res["flags"] = 0


class AdmomBootstrapper(Bootstrapper):
    _default_admom_pars = {
        "ntry": 4,  # number of times to retry fit with a new guess
        "maxiter": 200,  # max number of iterations in a fit
    }

    def __init__(self, obs, admom_pars=None, verbose=False, **kw):
        """
        The data can be mutated: If a PSF fit is performed, the gmix will be
        set for the input PSF observation

        parameters
        ----------
        obs: observation(s)
            Either an Observation, ObsList, or MultiBandObsList The
            Observations must have a psf set.

            If the psf observations already have gmix objects set, there is no
            need to run fit_psfs()
        """

        self.verbose = verbose
        self._set_admom_pars(admom_pars)

        # this never gets modified in any way
        self.mb_obs_list_orig = get_mb_obs(obs)

        # this will get replaced if fit_psfs is run
        self.mb_obs_list = self.mb_obs_list_orig

        self.model_fits = {}

    def _set_admom_pars(self, admom_pars):
        if admom_pars is None:
            admom_pars = {}
            admom_pars.update(AdmomBootstrapper._default_admom_pars)

        self._admom_pars = admom_pars

    def get_fitter(self):
        """
        get the adaptive moments fitter
        """
        if not hasattr(self, "fitter"):
            raise RuntimeError("you need to run fit() successfully first")
        return self.fitter

    def fit(self, Tguess=None):
        """
        pars controlling the adaptive moment fit are given on construction
        """
        if Tguess is None:
            Tguess = self._get_Tguess()

        fitter = self._fit_one(self.mb_obs_list, Tguess)
        res = fitter.get_result()

        if res["flags"] != 0:
            f = res["flags"]
            fs = res["flagstr"]
            raise BootGalFailure("admom gal fit failed: %d '%s'" % (f, fs))

        self.fitter = fitter

    def fit_psfs(
        self,
        Tguess=None,
        Tguess_key=None,
        skip_failed=True,
        skip_already_done=True,
    ):
        """
        Fit all psfs.  If the psf observations already have a gmix
        then this step is not necessary

        pars controlling the adaptive moment fit are given on construction

        parameters
        ----------
        Tguess: optional, float
            Guess for T
        Tguess_key: optional, string
            Get the T guess from the given key in the metadata
        skip_failed: bool
            If True, failures are just skipped when fitting the galaxy;
            in other words those observations will be ignored.  If False
            then an exception is raised
        skip_already_done: bool
            Skip psfs with a gmix already set
        """

        ntot = 0
        new_mb_obslist = MultiBandObsList()

        mb_obs_list = self.mb_obs_list
        for band, obslist in enumerate(mb_obs_list):
            new_obslist = ObsList()

            for i, obs in enumerate(obslist):
                if not obs.has_psf():
                    raise RuntimeError("observation does not have a psf set")

                try:

                    psf_obs = obs.get_psf()
                    if skip_already_done:
                        if psf_obs.has_gmix():
                            # if have a gmix, skip it
                            new_obslist.append(obs)
                            ntot += 1
                            continue

                    if Tguess_key is not None:
                        Tguess_i = psf_obs.meta[Tguess_key]
                    else:
                        if Tguess is not None:
                            Tguess_i = Tguess
                        else:
                            Tguess_i = self._get_psf_Tguess(psf_obs)

                    self._fit_one_psf(psf_obs, Tguess_i)

                    new_obslist.append(obs)
                    ntot += 1

                except BootPSFFailure as err:
                    if not skip_failed:
                        raise
                    else:
                        mess = (
                            "    failed psf fit band %d obs %d: '%s'.  "
                            "skipping observation" % (band, i, str(err))
                        )
                        print(mess)
                        continue

            new_mb_obslist.append(new_obslist)

        if ntot == 0:
            raise BootPSFFailure("no psf fits succeeded")

        self.mb_obs_list = new_mb_obslist

    def _fit_one_psf(self, obs, Tguess):
        """
        Fit the image and set the gmix attribute

        parameters
        ----------
        obs: Observation
            Single psf observation
        Tguess: float
            Initial guess; random guesses are generated based on this
        """
        fitter = self._fit_one(obs, Tguess)

        obs.update_meta_data({"fitter": fitter})

        res = fitter.get_result()
        if res["flags"] != 0:
            f = res["flags"]
            fs = res["flagstr"]
            raise BootPSFFailure("admom psf fit failed: %d '%s'" % (f, fs))

        gmix = fitter.get_gmix()
        obs.set_gmix(gmix)

    def _fit_one(self, obs, Tguess):
        """
        Fit the observation(s) using adaptive moments

        parameters
        ----------
        obs: observation
            A Observation, ObsList, or MultiBandObsList
        Tguess: float
            Initial guess; random guesses are generated based on this
        """
        from . import admom

        pars = self._admom_pars

        fitter = admom.Admom(obs, maxiter=pars["maxiter"])

        for i in range(pars["ntry"]):
            # this generates a gaussian mixture guess based on Tguess
            try:
                # should probably move this catch into admom
                fitter.go(Tguess)
                res = fitter.get_result()
            except GMixRangeError as err:
                print(str(err))
                res = {"flags": 1}

            if res["flags"] == 0:
                break

        if res["flags"] == 0:
            # for consistency
            res["g"] = res["e"]
            res["g_cov"] = res["e_cov"]

        return fitter

    def _set_flux(self, mb_obs_list, fitter):
        """
        do flux in each band separately
        """

        # for each band
        nband = len(mb_obs_list)
        res = fitter.get_result()
        res["flux"] = zeros(nband) - 9999
        res["flux_err"] = zeros(nband) + 9999
        res["flux_s2n"] = zeros(nband) - 9999

        try:
            gmix = fitter.get_gmix()

            for band, obs_list in enumerate(mb_obs_list):
                for obs in obs_list:
                    obs.set_gmix(gmix)

                flux_fitter = fitting.TemplateFluxFitter(obs_list)
                flux_fitter.go()

                fres = flux_fitter.get_result()
                if fres["flags"] != 0:
                    res["flags"] = fres
                    raise BootPSFFailure("could not get flux")

                res["flux"][band] = fres["flux"]
                res["flux_err"][band] = fres["flux_err"]

                if fres["flux_err"] > 0:
                    res["flux_s2n"][band] = fres["flux"] / fres["flux_err"]

        except GMixRangeError as err:
            raise BootPSFFailure(str(err))

    def _get_psf_Tguess(self, obs):
        scale = obs.jacobian.get_scale()
        return 4.0 * scale

    def _get_Tguess(self):

        ntot = 0
        Tsum = 0.0

        for obs_list in self.mb_obs_list:
            for obs in obs_list:
                psf_gmix = obs.get_psf_gmix()
                Tsum += psf_gmix.get_T()
                ntot += 1

        T = Tsum / ntot

        return 2.0 * T


class AdmomMetacalBootstrapper(AdmomBootstrapper):
    _default_metacal_pars = {
        "types": ["noshear", "1p", "1m", "2p", "2m"],
    }

    def __init__(self, obs, metacal_pars=None, **kw):
        super(AdmomMetacalBootstrapper, self).__init__(obs, **kw)
        self._set_metacal_pars(metacal_pars)

    def get_metacal_pars(self):
        p = {}
        p.update(self._metacal_pars)
        return p

    def _set_metacal_pars(self, parsin):
        metacal_pars = {}
        metacal_pars.update(AdmomMetacalBootstrapper._default_metacal_pars,)
        if parsin is not None:
            metacal_pars.update(parsin)

        self._metacal_pars = metacal_pars

    def get_metacal_result(self):
        """
        get result of metacal
        """
        if not hasattr(self, "metacal_res"):
            raise RuntimeError("you need to run fit_metacal first")
        return self.metacal_res

    def fit_metacal(self, Tguess=None, psf_Tguess=None, psf_Tguess_key=None):
        """
        pars controlling the adaptive moment fit are given on construction
        """

        obs_dict = metacal.get_all_metacal(
            self.mb_obs_list, **self._metacal_pars
        )

        # always process noshear first
        keys = list(obs_dict.keys())
        try:
            index = keys.index("noshear")
            keys.pop(index)
            keys = ["noshear"] + keys
        except ValueError:
            pass

        # overall flags, or'ed from each bootstrapper
        res = {"mcal_flags": 0}
        for key in keys:
            # run a regular Bootstrapper on these observations
            boot = AdmomBootstrapper(
                obs_dict[key],
                admom_pars=self._admom_pars,
                verbose=self.verbose,
            )

            if key == "noshear":
                boot.fit_psfs(
                    Tguess=psf_Tguess,
                    Tguess_key=psf_Tguess_key,
                    skip_failed=True,
                    # just in case we have a bookkeeping problem
                    skip_already_done=False,
                )

                wsum = 0.0
                Tpsf_sum = 0.0
                gpsf_sum = zeros(2)
                npsf = 0
                for obslist in boot.mb_obs_list:
                    for obs in obslist:
                        g1, g2, T = obs.psf.gmix.get_g1g2T()

                        # TODO we sometimes use other weights
                        twsum = obs.weight.sum()

                        wsum += twsum
                        gpsf_sum[0] += g1 * twsum
                        gpsf_sum[1] += g2 * twsum
                        Tpsf_sum += T * twsum
                        npsf += 1

                gpsf = gpsf_sum / wsum
                Tpsf = Tpsf_sum / wsum

                if Tguess is None:
                    Tguess = 2 * Tpsf

            boot.fit(Tguess=Tguess)

            # flags actually raise BootGalFailure from the AdmomBootstrapper
            # so this is not necessary
            tres = boot.get_fitter().get_result()

            # if tres['flags'] != 0:
            #    raise BootGalFailure("failed to fit galaxy with admom")

            # should be zero
            # res['mcal_flags'] |= tres['flags']

            if key == "noshear":
                tres["gpsf"] = gpsf
                tres["Tpsf"] = Tpsf

            res[key] = tres

        self.metacal_res = res


class MaxMetacalBootstrapper(Bootstrapper):
    def get_metacal_result(self):
        """
        get result of metacal
        """
        if not hasattr(self, "metacal_res"):
            raise RuntimeError("you need to run fit_metacal first")
        return self.metacal_res

    def fit_metacal(
        self,
        psf_model,
        gal_model,
        pars,
        psf_Tguess,
        psf_fit_pars=None,
        metacal_pars=None,
        prior=None,
        psf_ntry=5,
        ntry=1,
        guesser=None,
        **kw
    ):
        """
        run metacalibration

        parameters
        ----------
        psf_model: string
            model to fit for psf
        gal_model: string
            model to fit
        pars: dict
            parameters for the maximum likelihood fitter
        psf_Tguess: float
            T guess for psf
        psf_fit_pars: dict
            parameters for psf fit
        metacal_pars: dict, optional
            Parameters for metacal, default {'step':0.01}
        prior: prior on parameters, optional
            Optional prior to apply
        psf_ntry: int, optional
            Number of times to retry psf fitting, default 5
        ntry: int, optional
            Number of times to retry fitting, default 1
        **kw:
            extra keywords for get_all_metacal
        """

        obs_dict = self._get_all_metacal(metacal_pars, **kw)

        res = self._do_metacal_max_fits(
            obs_dict,
            psf_model,
            gal_model,
            pars,
            psf_Tguess,
            prior,
            psf_ntry,
            ntry,
            psf_fit_pars,
            guesser=guesser,
        )

        self.metacal_res = res

    def _get_all_metacal(self, metacal_pars, **kw):

        metacal_pars = self._extract_metacal_pars(metacal_pars)
        return metacal.get_all_metacal(self.mb_obs_list, **metacal_pars)

    def _extract_metacal_pars(self, metacal_pars_in):
        """
        make sure at least the step is specified
        """
        metacal_pars = {"step": 0.01}

        if metacal_pars_in is not None:
            metacal_pars.update(metacal_pars_in)

        return metacal_pars

    def _do_metacal_max_fits(
        self,
        obs_dict,
        psf_model,
        gal_model,
        pars,
        psf_Tguess,
        prior,
        psf_ntry,
        ntry,
        psf_fit_pars,
        guesser=None,
    ):

        # overall flags, or'ed from each bootstrapper
        res = {"mcal_flags": 0}
        for key in sorted(obs_dict):
            # run a regular Bootstrapper on these observations
            boot = Bootstrapper(obs_dict[key], verbose=self.verbose)

            boot.fit_psfs(
                psf_model,
                psf_Tguess,
                ntry=psf_ntry,
                fit_pars=psf_fit_pars,
                skip_already_done=False,
            )
            boot.fit_max(
                gal_model, pars, guesser=guesser, prior=prior, ntry=ntry,
            )

            tres = boot.get_max_fitter().get_result()

            res["mcal_flags"] |= tres["flags"]

            wsum = 0.0
            Tpsf_sum = 0.0
            gpsf_sum = zeros(2)
            npsf = 0
            for obslist in boot.mb_obs_list:
                for obs in obslist:
                    if hasattr(obs, "psf_nopix"):
                        # print("    summing nopix")
                        g1, g2, T = obs.psf_nopix.gmix.get_g1g2T()
                    else:
                        g1, g2, T = obs.psf.gmix.get_g1g2T()

                    # TODO we sometimes use other weights
                    twsum = obs.weight.sum()

                    wsum += twsum
                    gpsf_sum[0] += g1 * twsum
                    gpsf_sum[1] += g2 * twsum
                    Tpsf_sum += T * twsum
                    npsf += 1

            tres["gpsf"] = gpsf_sum / wsum
            tres["Tpsf"] = Tpsf_sum / wsum

            res[key] = tres

        return res


class MetacalAnalyticPSFBootstrapper(MaxMetacalBootstrapper):
    def _get_all_metacal(self, metacal_pars, **kw):

        metacal_pars = self._extract_metacal_pars(metacal_pars)

        # noshear gets added in automatically when doing 1p
        # if 'types' not in metacal_pars:
        #    metacal_pars['types'] = kw.get('types', [ '1p','1m','2p','2m' ])

        # the psf to use for metacal, currently structurally same for all
        psf = kw.pop("psf", None)
        if psf is None:
            # we will create it internally
            psf = metacal_pars["analytic_psf"]
            # raise ValueError('expected psf= for analytic psf')

        odict = metacal.get_all_metacal(
            self.mb_obs_list, psf=psf, **metacal_pars
        )
        return odict


class BDFBootstrapper(Bootstrapper):
    def fit_max(
        self,
        max_pars,
        guess=None,
        guess_widths=None,
        prior=None,
        ntry=1,
        obs=None,
    ):
        """
        fit the galaxy.  You must run fit_psf() successfully first
        """

        assert prior is not None
        assert guess is None, "for now"

        if obs is None:
            obs = self.mb_obs_list

        if not hasattr(self, "psf_flux_res"):
            self.fit_gal_psf_flux()

        guesser = self._get_guesser(prior)

        runner = BDFRunner(obs, max_pars, guesser, prior=prior,)

        runner.go(ntry=ntry)

        fitter = runner.fitter

        res = fitter.get_result()

        if res["flags"] != 0:
            raise BootGalFailure("failed to fit galaxy with maxlike")

        self.max_fitter = fitter

    def _get_guesser(self, prior):
        """
        get a guesser that uses the psf T and galaxy psf flux to
        generate a guess, drawing from priors on the other parameters
        """
        from .guessers import BDFGuesser

        psf_T = self.mb_obs_list[0][0].psf.gmix.get_T()

        pres = self.get_psf_flux_result()

        return BDFGuesser(
            psf_T,
            pres["psf_flux"],
            # 1.0,
            prior,
        )


class PSFRunner(object):
    """
    wrapper to generate guesses and run the psf fitter a few times
    """

    def __init__(self, obs, model, Tguess, lm_pars, prior=None, rng=None):

        self.obs = obs
        self.prior = prior
        self.set_rng(rng)

        mess = "psf model should be turb or gauss,got '%s'" % model
        assert model in ["turb", "gauss"], mess

        self.model = model
        self.lm_pars = lm_pars
        self.set_guess0(Tguess)

    def go(self, ntry=1, guess=None):

        from .fitting import LMSimple

        fitter = LMSimple(
            model=self.model,
            prior=self.prior,
            fit_pars=self.lm_pars,
        )
        for i in range(ntry):

            if i == 0 and guess is not None:
                this_guess = guess.copy()
            else:
                this_guess = self.get_guess()

            fitter.go(obs=self.obs, guess=this_guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        self.fitter = fitter

    def get_guess(self):
        rng = self.rng

        guess = self.guess0.copy()

        guess[0:0 + 2] += rng.uniform(low=-0.01, high=0.01, size=2)
        guess[2:2 + 2] += rng.uniform(low=-0.1, high=0.1, size=2)
        guess[4] = guess[4] * (1.0 + rng.uniform(low=-0.1, high=0.1))
        guess[5] = guess[5] * (1.0 + rng.uniform(low=-0.1, high=0.1))

        return guess

    def set_guess0(self, Tguess):
        Fguess = self.obs.image.sum()
        Fguess *= self.obs.jacobian.get_scale() ** 2
        self.guess0 = array([0.0, 0.0, 0.0, 0.0, Tguess, Fguess])

    def set_rng(self, rng):
        if rng is None:
            rng = np.random.RandomState()

        self.rng = rng


class AMRunner(object):
    """
    wrapper to run am
    """

    def __init__(self, obs, Tguess, rng=None):

        self.obs = obs
        self.Tguess = Tguess
        self.rng = rng

    def get_fitter(self):
        return self.fitter

    def go(self, ntry=1):

        for i in range(ntry):

            fitter = admom.run_admom(self.obs, self.Tguess, rng=self.rng)
            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter


class PSFRunnerEM(object):
    """
    wrapper to generate guesses and run the psf fitter a few times. Does
    not support running in fixed center or fluxonly modes

    The guesses are tuned for fitting PSFs, with the centers all near the
    jacobian center

    Parameters
    ----------
    obs: ngmix.Observation
        The observation to fit
    Tguess: float
        The guess for the overall T
    ngauss: int
        Number of gaussians, 1 to 5
    em_pars: dict
        Can have entries
        miniter: The minimum number of iterations
        maxiter: The maximum number of iterations
        tol: The fractional change in the log likelihood that implies convergence
        vary_sky: If True, fit for the sky level
    rng: np.random.RandomState
        A random number generator, used for generating guesses
    """

    def __init__(self, *,
                 obs,
                 Tguess,
                 ngauss,
                 em_pars=None,
                 rng=None):

        self.ngauss = ngauss
        self.Tguess = Tguess
        self.sigma_guess = sqrt(Tguess / 2)
        self.set_obs(obs)

        if em_pars is None:
            em_pars = {}
        self.em_pars = em_pars
        self.set_rng(rng)

    def set_obs(self, obsin):
        """
        set a new observation with sky
        """
        self.obs, self.sky = em.prep_obs(obsin)

    def get_fitter(self):
        """
        get the fitter used for the processing

        Returns
        --------
        ngmix.em.GMixEM
            The fitter
        """
        return self.fitter

    def go(self, ntry=1, guess=None):
        """
        run the fitter

        Parameters
        ----------
        ntry: int
            Number of times to try the fit, Default 1
        guess: ngmix.GMix
            Use the input mixture for the first guess

        Returns
        -------
        None
        """
        fitter = em.GMixEM(self.obs, **self.em_pars)
        for i in range(ntry):

            if i == 0 and guess is not None:
                this_guess = guess.copy()
            else:
                this_guess = self.get_guess()

            fitter.go(this_guess, self.sky)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter

    def get_guess(self):
        """
        Get a guess for the EM algorithm

        Returns
        -------
        ngmix.GMix
            The guess mixture, with the number of gaussians
            as specified in the constructor
        """

        if self.ngauss == 1:
            return self._get_em_guess_1gauss()
        elif self.ngauss == 2:
            return self._get_em_guess_2gauss()
        elif self.ngauss == 3:
            return self._get_em_guess_3gauss()
        elif self.ngauss == 4:
            return self._get_em_guess_4gauss()
        elif self.ngauss == 5:
            return self._get_em_guess_5gauss()
        else:
            raise ValueError("bad ngauss: %d" % self.ngauss)

    def _get_em_guess_1gauss(self):
        rng = self.rng

        sigma2 = self.sigma_guess ** 2
        pars = array(
            [
                1.0 + rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                sigma2 * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.2 * sigma2, high=0.2 * sigma2),
                sigma2 * (1.0 + rng.uniform(low=-0.1, high=0.1)),
            ]
        )

        return GMix(pars=pars)

    def _get_em_guess_2gauss(self):
        rng = self.rng

        sigma2 = self.sigma_guess ** 2

        pars = array(
            [
                _em2_pguess[0],
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em2_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                0.0,
                _em2_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em2_pguess[1],
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em2_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                0.0,
                _em2_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
            ]
        )

        return GMix(pars=pars)

    def _get_em_guess_3gauss(self):
        rng = self.rng

        sigma2 = self.sigma_guess ** 2

        pars = array(
            [
                _em3_pguess[0] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em3_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em3_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em3_pguess[1] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em3_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em3_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em3_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em3_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em3_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
            ]
        )

        return GMix(pars=pars)

    def _get_em_guess_4gauss(self):
        rng = self.rng

        sigma2 = self.sigma_guess ** 2

        pars = array(
            [
                _em4_pguess[0] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em4_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em4_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em4_pguess[1] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em4_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em4_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em4_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em4_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em4_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em4_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em4_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em4_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
            ]
        )

        return GMix(pars=pars)

    def _get_em_guess_5gauss(self):
        rng = self.rng

        sigma2 = self.sigma_guess ** 2

        pars = array(
            [
                _em5_pguess[0] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em5_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em5_fguess[0]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em5_pguess[1] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em5_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em5_fguess[1]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em5_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em5_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                _em5_pguess[2] * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.1, high=0.1),
                rng.uniform(low=-0.1, high=0.1),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
                rng.uniform(low=-0.01, high=0.01),
                _em5_fguess[2]
                * sigma2
                * (1.0 + rng.uniform(low=-0.1, high=0.1)),
            ]
        )

        return GMix(pars=pars)

    def set_rng(self, rng):
        if rng is None:
            rng = np.random.RandomState()

        self.rng = rng


class PSFRunnerCoellip(object):
    """
    wrapper to generate guesses and run the psf fitter a few times
    """

    def __init__(self, obs, Tguess, ngauss, lm_pars, rng=None):

        self.obs = obs
        self.set_rng(rng)

        self.ngauss = ngauss
        self.npars = get_coellip_npars(ngauss)
        self.model = "coellip"
        self.lm_pars = lm_pars
        self.set_guess0(Tguess)
        self._set_prior()

    def get_fitter(self):
        return self.fitter

    def go(self, ntry=1, guess=None):
        from .fitting import LMCoellip

        for i in range(ntry):

            if i == 0 and guess is not None:
                this_guess = guess.copy()
            else:
                this_guess = self.get_guess()

            fitter = LMCoellip(
                self.obs, self.ngauss, lm_pars=self.lm_pars, prior=self.prior
            )

            fitter.go(this_guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        self.fitter = fitter

    def get_guess(self):

        rng = self.rng

        guess = np.zeros(self.npars)

        guess[0:0 + 2] += rng.uniform(low=-0.01, high=0.01, size=2)
        guess[2:2 + 2] += rng.uniform(low=-0.05, high=0.05, size=2)

        fac = 0.01
        if self.ngauss == 1:
            guess[4] = self.Tguess * (1.0 + rng.uniform(low=-0.1, high=0.1))
            guess[5] = self.Fguess * (1.0 + rng.uniform(low=-0.1, high=0.1))
        elif self.ngauss == 2:
            guess[4] = (
                self.Tguess
                * _moffat2_fguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[5] = (
                self.Tguess
                * _moffat2_fguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

            guess[6] = (
                self.Fguess
                * _moffat2_pguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[7] = (
                self.Fguess
                * _moffat2_pguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

        elif self.ngauss == 3:
            guess[4] = (
                self.Tguess
                * _moffat3_fguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[5] = (
                self.Tguess
                * _moffat3_fguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[6] = (
                self.Tguess
                * _moffat3_fguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

            guess[7] = (
                self.Fguess
                * _moffat3_pguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[8] = (
                self.Fguess
                * _moffat3_pguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[9] = (
                self.Fguess
                * _moffat3_pguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

        elif self.ngauss == 4:
            guess[4] = (
                self.Tguess
                * _moffat4_fguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[5] = (
                self.Tguess
                * _moffat4_fguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[6] = (
                self.Tguess
                * _moffat4_fguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[7] = (
                self.Tguess
                * _moffat4_fguess[3]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

            guess[8] = (
                self.Fguess
                * _moffat4_pguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[9] = (
                self.Fguess
                * _moffat4_pguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[10] = (
                self.Fguess
                * _moffat4_pguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[11] = (
                self.Fguess
                * _moffat4_pguess[3]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

        elif self.ngauss == 5:
            guess[4] = (
                self.Tguess
                * _moffat5_fguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[5] = (
                self.Tguess
                * _moffat5_fguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[6] = (
                self.Tguess
                * _moffat5_fguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[7] = (
                self.Tguess
                * _moffat5_fguess[3]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[8] = (
                self.Tguess
                * _moffat5_fguess[4]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

            guess[9] = (
                self.Fguess
                * _moffat5_pguess[0]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[10] = (
                self.Fguess
                * _moffat5_pguess[1]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[11] = (
                self.Fguess
                * _moffat5_pguess[2]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[12] = (
                self.Fguess
                * _moffat5_pguess[3]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )
            guess[13] = (
                self.Fguess
                * _moffat5_pguess[4]
                * (1.0 + rng.uniform(low=-fac, high=fac))
            )

        else:
            raise RuntimeError("ngauss should be 1,2,3,4")

        return guess

    def set_guess0(self, Tguess):

        self.pixel_scale = self.obs.jacobian.get_scale()
        self.Tguess = Tguess
        Fguess = self.obs.image.sum()
        Fguess *= self.pixel_scale ** 2

        self.Fguess = Fguess

    def _set_prior(self):
        from .joint_prior import PriorCoellipSame
        from .priors import CenPrior, ZDisk2D, TwoSidedErf

        rng = self.rng

        Tguess = self.Tguess
        Fguess = self.Fguess

        cen_width = 2 * self.pixel_scale
        cen_prior = CenPrior(0.0, 0.0, cen_width, cen_width, rng=rng)
        g_prior = ZDisk2D(1.0, rng=rng)
        T_prior = TwoSidedErf(
            0.01 * Tguess, 0.001 * Tguess, 100 * Tguess, Tguess, rng=rng
        )
        F_prior = TwoSidedErf(
            0.01 * Fguess, 0.001 * Fguess, 100 * Fguess, Fguess, rng=rng
        )

        self.prior = PriorCoellipSame(
            self.ngauss, cen_prior, g_prior, T_prior, F_prior
        )

    def set_rng(self, rng):
        if rng is None:
            rng = np.random.RandomState()

        self.rng = rng


_moffat2_pguess = array([0.5, 0.5])
_moffat2_fguess = array([0.48955064, 1.50658978])

_moffat3_pguess = array([0.27559669, 0.55817131, 0.166232])
_moffat3_fguess = array([0.36123609, 0.8426139, 2.58747785])

_moffat4_pguess = array([0.44534, 0.366951, 0.10506, 0.0826497])
_moffat4_fguess = array([0.541019, 1.19701, 0.282176, 3.51086])

_moffat5_pguess = array([0.45, 0.25, 0.15, 0.1, 0.05])
_moffat5_fguess = array([0.541019, 1.19701, 0.282176, 3.51086])

_moffat5_pguess = array(
    [0.57874897, 0.32273483, 0.03327272, 0.0341253, 0.03111819]
)
_moffat5_fguess = array(
    [0.27831284, 0.9959897, 5.86989779, 5.63590429, 4.17285878]
)

# _moffat3_pguess=array([0.45, 0.45, 0.1])
# _moffat3_fguess=array([0.48955064,  1.50658978, 3.0])


class MaxRunner(object):
    """
    wrapper to generate guesses and run the fitter a few times
    """

    def __init__(self, obs, model, max_pars, guesser, prior=None):

        self.obs = obs

        self.max_pars = max_pars.copy()
        self.method = max_pars.pop("method", "lm")
        if self.method == "lm":
            self.send_pars = max_pars["lm_pars"]

        mess = "model should be exp,dev,gauss, got '%s'" % model
        assert model in ["exp", "dev", "gauss"], mess

        self.model = model
        self.prior = prior

        self.guesser = guesser

    def get_fitter(self):
        return self.fitter

    def go(self, ntry=1):
        if self.method == "lm":
            method = self._go_lm
        else:
            method = self._go_max

        method(ntry=ntry)

    def _go_lm(self, ntry=1):

        fitclass = self._get_lm_fitter_class()

        for i in range(ntry):
            guess = self.guesser()
            fitter = fitclass(
                self.obs, self.model, lm_pars=self.send_pars, prior=self.prior,
            )

            fitter.go(guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter

    def _go_max(self, ntry=1):

        fitclass = self._get_max_fitter_class()

        for i in range(ntry):
            guess = self.guesser()
            fitter = fitclass(
                self.obs, self.model, prior=self.prior, **self.max_pars
            )

            fitter.go(guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter

    def _get_lm_fitter_class(self):
        from .fitting import LMSimple

        return LMSimple

    def _get_max_fitter_class(self):
        from .fitting import MaxSimple

        return MaxSimple


class BDRunner(MaxRunner):
    """
    wrapper to generate guesses and run the BD fitter a few times
    """

    def __init__(self, obs, max_pars, guesser, prior=None):
        self.obs = obs

        self.max_pars = max_pars

        self.method = max_pars.get("method", "lm")
        if self.method == "lm":
            self.send_pars = max_pars["lm_pars"]
        else:
            self.send_pars = max_pars

        self.prior = prior

        self.guesser = guesser

    def _go_lm(self, ntry=1):
        fitclass = self._get_lm_fitter_class()

        for i in range(ntry):
            guess = self.guesser()
            fitter = fitclass(
                self.obs, lm_pars=self.send_pars, prior=self.prior,
            )

            fitter.go(guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter

    def _get_lm_fitter_class(self):
        from .fitting import LMBD

        return LMBD


class BDFRunner(MaxRunner):
    """
    wrapper to generate guesses and run the BDF fitter a few times
    """

    def __init__(self, obs, max_pars, guesser, prior=None):
        self.obs = obs

        self.max_pars = max_pars

        self.method = max_pars.get("method", "lm")
        if self.method == "lm":
            self.send_pars = max_pars["lm_pars"]
        else:
            self.send_pars = max_pars

        self.prior = prior

        self.guesser = guesser

    def _go_lm(self, ntry=1):
        fitclass = self._get_lm_fitter_class()

        for i in range(ntry):
            guess = self.guesser()
            fitter = fitclass(
                self.obs, lm_pars=self.send_pars, prior=self.prior,
            )

            fitter.go(guess)

            res = fitter.get_result()
            if res["flags"] == 0:
                break

        res["ntry"] = i + 1
        self.fitter = fitter

    def _get_lm_fitter_class(self):
        from .fitting import LMBDF

        return LMBDF


def get_em_ngauss(name):
    ngauss = int(name[2:])
    return ngauss


def get_coellip_ngauss(name):
    ngauss = int(name[7:])
    return ngauss


def replace_masked_pixels(
    mb_obs_list, inplace=False, method="best-fit", fitter=None, add_noise=False
):
    """
    replaced masked pixels

    The original image is stored for each Observation as .image_orig

    parameters
    ----------
    mb_obs_list: MultiBandObsList
        The original observations
    inplace: bool
        If True, modify the data in place.  Default False; a full
        copy is made.
    method: string, optional
        Method for replacement.  Supported methods are 'best-fit'.
        Default is 'best-fit'
    fitter:
        when method=='best-fit', a fitter from fitting.py

    add_noise: bool
        If True, add noise to the replaced pixels based on the median
        noise in the image, derived from the weight map
    """

    assert method == "best-fit", "only best-fit replacement is supported"
    assert fitter is not None, "fitter required"

    if inplace:
        mbo = mb_obs_list
    else:
        mbo = deepcopy(mb_obs_list)

    nband = len(mbo)

    for band in range(nband):
        olist = mbo[band]
        for iobs, obs in enumerate(olist):

            im = obs.image

            if obs.has_bmask():
                bmask = obs.bmask
            else:
                bmask = None

            if hasattr(obs, "weight_raw"):
                # print("    using raw weight for replace")
                weight = obs.weight_raw
            else:
                weight = obs.weight

            if bmask is not None:
                w = where((bmask != 0) | (weight == 0.0))
            else:
                w = where(weight == 0.0)

            if w[0].size > 0:
                print(
                    "        replacing %d/%d masked or zero weight "
                    "pixels" % (w[0].size, im.size)
                )
                obs.image_orig = obs.image.copy()
                gm = fitter.get_convolved_gmix(band=band, obsnum=iobs)

                im = obs.image
                model_image = gm.make_image(im.shape, jacobian=obs.jacobian)

                im[w] = model_image[w]

                if add_noise:
                    wgood = where(weight > 0.0)
                    if wgood[0].size > 0:
                        median_err = np.median(1.0 / weight[wgood])

                        noise_image = np.random.normal(
                            loc=0.0, scale=median_err, size=im.shape
                        )

                        im[w] += noise_image[w]

            else:
                obs.image_orig = None

    return mbo


_em2_pguess = array([0.596510042804182, 0.4034898268889178])
_em2_fguess = array([0.5793612389470884, 1.621860687127999])

_em3_pguess = array(
    [0.596510042804182, 0.4034898268889178, 1.303069003078001e-07]
)
_em3_fguess = array([0.5793612389470884, 1.621860687127999, 7.019347162356363])

_em4_pguess = array(
    [0.596510042804182, 0.4034898268889178, 1.303069003078001e-07, 1.0e-8]
)
_em4_fguess = array(
    [0.5793612389470884, 1.621860687127999, 7.019347162356363, 16.0]
)

_em5_pguess = array(
    [0.59453032, 0.35671819, 0.03567182, 0.01189061, 0.00118906]
)
_em5_fguess = array([0.5, 1.0, 3.0, 10.0, 20.0])

# _em3_pguess = array([0.7189864,0.2347828,0.04623086])
# _em3_fguess = array([0.4431912,1.354587,8.274546])
# _em3_pguess = array([0.60,0.36,0.04])
# _em3_fguess = array([0.58,1.62,3.0])


def test_boot(model, **keys):
    from .test import make_test_observations

    psf_obs, obs = make_test_observations(model, **keys)

    obs.set_psf(psf_obs)

    boot = Bootstrapper(obs)

    psf_model = keys.get("psf_model", "gauss")
    Tguess = 4.0
    boot.fit_psfs(psf_model, Tguess)

    pars = {"method": "lm", "lm_pars": {"maxfev": 4000}}
    boot.fit_max(model, pars)


def demo_psfrunner_em(show=False):
    from .jacobian import DiagonalJacobian
    from .observation import Observation

    rng = np.random.RandomState(8821)

    pixel_scale = 0.263
    dims = [25, 25]
    cen = (np.array(dims) - 1.0) / 2.0

    jacob = DiagonalJacobian(scale=pixel_scale, row=cen[0], col=cen[1])

    Tpsf = 0.27
    psf_gm = GMixModel([0.0, 0.0, 0.0, 0.0, Tpsf, 1.0], "turb")
    # psf_gm = GMixModel([0.0, 0.0, 0.0, 0.0, Tpsf, 1.0], "gauss")
    psf_im = psf_gm.make_image(dims, jacobian=jacob)

    psf_obs = Observation(psf_im, jacobian=jacob)

    Tguess = psf_gm.get_T() * rng.uniform(low=-0.9, high=1.1)

    runner = PSFRunnerEM(
        obs=psf_obs, Tguess=Tguess, ngauss=3, rng=rng,
        em_pars={'tol': 1.0e-5, 'maxiter': 10000},
    )
    runner.go()

    fitter = runner.get_fitter()
    res = fitter.get_result()
    print(res)
    assert res['flags'] == 0

    if show:
        try:
            import images
        except ImportError:
            from espy import images

        imfit = fitter.make_image()

        maxdiff = np.abs(imfit - psf_obs.image).max()
        print('maxdiff:', maxdiff)
        images.compare_images(psf_obs.image, imfit)
