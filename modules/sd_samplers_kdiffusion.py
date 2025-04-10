import torch
import inspect
from modules import sd_samplers_common, sd_samplers_extra, sd_samplers_cfg_denoiser, sd_schedulers
from modules.sd_samplers_cfg_denoiser import CFGDenoiser  # noqa: F401
from modules.script_callbacks import ExtraNoiseParams, extra_noise_callback
import modules.sd_samplers_kdiffusion_smea as sd_samplers_kdiffusion_smea

from modules.shared import opts
import modules.shared as shared
from modules_forge.forge_sampler import sampling_prepare, sampling_cleanup

if opts.sd_sampling == "A1111":
    import k_diffusion
    from k_diffusion import sampling
    from k_diffusion.external import CompVisDenoiser, CompVisVDenoiser
elif opts.sd_sampling == "ldm patched (Comfy)":
    import ldm_patched.k_diffusion
    from ldm_patched.k_diffusion import sampling
    from ldm_patched.k_diffusion.external import CompVisDenoiser, CompVisVDenoiser


samplers_k_diffusion = [
    ('DPM++ 2M', 'sample_dpmpp_2m', ['k_dpmpp_2m'], {'scheduler': 'karras'}),
    ('DPM++ SDE', 'sample_dpmpp_sde', ['k_dpmpp_sde'], {'scheduler': 'karras', "second_order": True, "brownian_noise": True}),
    ('DPM++ 2M SDE', 'sample_dpmpp_2m_sde', ['k_dpmpp_2m_sde'], {'scheduler': 'exponential', "brownian_noise": True}),
    ('DPM++ 2M SDE Heun', 'sample_dpmpp_2m_sde', ['k_dpmpp_2m_sde_heun'], {'scheduler': 'exponential', "brownian_noise": True, "solver_type": "heun"}),
    ('DPM++ 2S a', 'sample_dpmpp_2s_ancestral', ['k_dpmpp_2s_a'], {'scheduler': 'karras', "uses_ensd": True, "second_order": True}),
    ('DPM++ 3M SDE', 'sample_dpmpp_3m_sde', ['k_dpmpp_3m_sde'], {'scheduler': 'exponential', 'discard_next_to_last_sigma': True, "brownian_noise": True}),
    ('Euler a', 'sample_euler_ancestral', ['k_euler_a', 'k_euler_ancestral'], {"uses_ensd": True}),
    ('Euler', 'sample_euler', ['k_euler'], {}),
    ('LMS', 'sample_lms', ['k_lms'], {}),
    ('Heun', 'sample_heun', ['k_heun'], {"second_order": True}),
    ('DPM2', 'sample_dpm_2', ['k_dpm_2'], {'scheduler': 'karras', 'discard_next_to_last_sigma': True, "second_order": True}),
    ('DPM2 a', 'sample_dpm_2_ancestral', ['k_dpm_2_a'], {'scheduler': 'karras', 'discard_next_to_last_sigma': True, "uses_ensd": True, "second_order": True}),
    ('DPM fast', 'sample_dpm_fast', ['k_dpm_fast'], {"uses_ensd": True}),
    ('DPM adaptive', 'sample_dpm_adaptive', ['k_dpm_ad'], {"uses_ensd": True}),
    ('Restart', sd_samplers_extra.restart_sampler, ['restart'], {'scheduler': 'karras', "second_order": True}),
]

additional_samplers = [
    ('Euler Dy', 'sample_euler_dy', ['k_euler_dy'], {}),
    ('Euler SMEA Dy', 'sample_euler_smea_dy', ['k_euler_smea_dy'], {}),
    ('Euler Negative', 'sample_euler_negative', ['k_euler_negative'], {}),
    ('Euler Negative Dy', 'sample_euler_dy_negative', ['k_euler_dy_negative'], {}),
    # ('Kohaku_LoNyu_Yog', 'sample_Kohaku_LoNyu_Yog', ['k_euler_dy_negative'], {}),
]
samplers_k_diffusion.extend(additional_samplers)

samplers_data_k_diffusion = [
    sd_samplers_common.SamplerData(label, lambda model, funcname=funcname: KDiffusionSampler(funcname, model), aliases, options)
    for label, funcname, aliases, options in samplers_k_diffusion
    if callable(funcname) or hasattr(sampling, funcname) or hasattr(sd_samplers_kdiffusion_smea, funcname)
]

sampler_extra_params = {
    'sample_euler': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_euler_ancestral': ['eta', 's_noise'],
    'sample_heun': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_dpm_2': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_dpm_fast': ['s_noise'],
    'sample_dpm_2_ancestral': ['s_noise'],
    'sample_dpmpp_2s_ancestral': ['eta', 's_noise'],
    'sample_dpmpp_sde': ['eta', 's_noise', 'r'],
    'sample_dpmpp_2m_sde': ['eta', 's_noise', 'solver_type'],
    'sample_dpmpp_3m_sde': ['eta', 's_noise'],
}

sampler_extra_params.update({
    'sample_euler_dy': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_euler_smea_dy': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_euler_negative': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_euler_dy_negative': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
})

k_diffusion_samplers_map = {x.name: x for x in samplers_data_k_diffusion}
k_diffusion_scheduler = {x.name: x.function for x in sd_schedulers.schedulers}


class CFGDenoiserKDiffusion(sd_samplers_cfg_denoiser.CFGDenoiser):
    @property
    def inner_model(self):
        if self.model_wrap is None:
            denoiser_constructor = getattr(shared.sd_model, 'create_denoiser', None)

            if denoiser_constructor is not None:
                self.model_wrap = denoiser_constructor()
            else:
                denoiser = CompVisVDenoiser if shared.sd_model.parameterization == "v" else CompVisDenoiser
                self.model_wrap = denoiser(shared.sd_model, quantize=shared.opts.enable_quantization)

        return self.model_wrap

    @property
    def latent_image(self):
        return getattr(self, '_latent_image', None)

    @latent_image.setter
    def latent_image(self, value):
        self._latent_image = value

    @latent_image.deleter
    def latent_image(self):
        if hasattr(self, '_latent_image'):
            del self._latent_image

    @property
    def noise(self):
        return getattr(self, '_noise', None)

    @noise.setter
    def noise(self, value):
        self._noise = value

    @noise.deleter
    def noise(self):
        if hasattr(self, '_noise'):
            del self._noise


class KDiffusionSampler(sd_samplers_common.Sampler):
    def __init__(self, funcname, sd_model, options=None):
        super().__init__(funcname)
        self.extra_params = sampler_extra_params.get(funcname, [])
        
        self.options = options or {}
        if callable(funcname):
            self.func = funcname
        elif hasattr(sampling, funcname):
            self.func = getattr(sampling, funcname)
        elif hasattr(sd_samplers_kdiffusion_smea, funcname):
            self.func = getattr(sd_samplers_kdiffusion_smea, funcname)
        else:
            raise ValueError(f"Sampler {funcname} not found in k_diffusion.sampling or sd_samplers_kdiffusion_smea")

        self.model_wrap_cfg = CFGDenoiserKDiffusion(self)
        self.model_wrap = self.model_wrap_cfg.inner_model

    def get_sigmas(self, p, steps):
        discard_next_to_last_sigma = self.config is not None and self.config.options.get('discard_next_to_last_sigma', False)
        if opts.always_discard_next_to_last_sigma and not discard_next_to_last_sigma:
            discard_next_to_last_sigma = True
            p.extra_generation_params["Discard penultimate sigma"] = True

        steps += 1 if discard_next_to_last_sigma else 0

        scheduler_name = (p.hr_scheduler if p.is_hr_pass else p.scheduler) or 'Automatic'
        if scheduler_name == 'Automatic':
            scheduler_name = self.config.options.get('scheduler', None)

        scheduler = sd_schedulers.schedulers_map.get(scheduler_name)

        m_sigma_min, m_sigma_max = self.model_wrap.sigmas[0].item(), self.model_wrap.sigmas[-1].item()
        sigma_min, sigma_max = (0.1, 10) if opts.use_old_karras_scheduler_sigmas else (m_sigma_min, m_sigma_max)

        if p.sampler_noise_scheduler_override:
            sigmas = p.sampler_noise_scheduler_override(steps)
        elif scheduler is None or scheduler.function is None:
            sigmas = self.model_wrap.get_sigmas(steps)
        else:
            sigmas_kwargs = {'sigma_min': sigma_min, 'sigma_max': sigma_max}

            if scheduler.label != 'Automatic' and not p.is_hr_pass:
                p.extra_generation_params["Schedule type"] = scheduler.label

            elif scheduler.label != p.extra_generation_params.get("Schedule type"):
                p.extra_generation_params["Hires schedule type"] = scheduler.label

            if opts.sigma_min != 0 and opts.sigma_min != m_sigma_min:
                sigmas_kwargs['sigma_min'] = opts.sigma_min
                p.extra_generation_params["Schedule min sigma"] = opts.sigma_min
            if opts.sigma_max != 0 and opts.sigma_max != m_sigma_max:
                sigmas_kwargs['sigma_max'] = opts.sigma_max
                p.extra_generation_params["Schedule max sigma"] = opts.sigma_max

            if scheduler.default_rho != -1 and opts.rho != 0 and opts.rho != scheduler.default_rho:
                sigmas_kwargs['rho'] = opts.rho
                p.extra_generation_params["Schedule rho"] = opts.rho

            if scheduler.need_inner_model:
                sigmas_kwargs['inner_model'] = self.model_wrap

            if scheduler.label == 'Beta':
                p.extra_generation_params["Beta schedule alpha"] = opts.beta_dist_alpha
                p.extra_generation_params["Beta schedule beta"] = opts.beta_dist_beta

            sigmas = scheduler.function(n=steps, **sigmas_kwargs, device=shared.device)

        if discard_next_to_last_sigma:
            sigmas = torch.cat([sigmas[:-2], sigmas[-1:]])

        return sigmas.cpu()

    def sample_img2img(self, p, x, noise, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        unet_patcher = self.model_wrap.inner_model.forge_objects.unet
        sampling_prepare(self.model_wrap.inner_model.forge_objects.unet, x=x)

        self.model_wrap.log_sigmas = self.model_wrap.log_sigmas.to(x.device)
        self.model_wrap.sigmas = self.model_wrap.sigmas.to(x.device)

        steps, t_enc = sd_samplers_common.setup_img2img_steps(p, steps)

        sigmas = self.get_sigmas(p, steps).to(x.device)
        sigma_sched = sigmas[steps - t_enc - 1:]

        x = x.to(noise)
        xi = x + noise * sigma_sched[0]

        if opts.img2img_extra_noise > 0:
            p.extra_generation_params["Extra noise"] = opts.img2img_extra_noise
            extra_noise_params = ExtraNoiseParams(noise, x, xi)
            extra_noise_callback(extra_noise_params)
            noise = extra_noise_params.noise
            xi += noise * opts.img2img_extra_noise

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'sigma_min' in parameters:
            ## last sigma is zero which isn't allowed by DPM Fast & Adaptive so taking value before last
            extra_params_kwargs['sigma_min'] = sigma_sched[-2]
        if 'sigma_max' in parameters:
            extra_params_kwargs['sigma_max'] = sigma_sched[0]
        if 'n' in parameters:
            extra_params_kwargs['n'] = len(sigma_sched) - 1
        if 'sigma_sched' in parameters:
            extra_params_kwargs['sigma_sched'] = sigma_sched
        if 'sigmas' in parameters:
            extra_params_kwargs['sigmas'] = sigma_sched

        if self.config.options.get('brownian_noise', False):
            noise_sampler = self.create_noise_sampler(x, sigmas, p)
            extra_params_kwargs['noise_sampler'] = noise_sampler

        if opts.sd_sampling == "A1111":
            if self.config.options.get('solver_type', None) == 'heun':
                extra_params_kwargs['solver_type'] = 'heun'
        else:
            pass

        self.model_wrap_cfg.init_latent = x
        self.last_latent = x
        self.sampler_extra_args = {
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }

        samples = self.launch_sampling(t_enc + 1, lambda: self.func(self.model_wrap_cfg, xi, extra_args=self.sampler_extra_args, disable=False, callback=self.callback_state, **extra_params_kwargs))

        self.add_infotext(p)

        sampling_cleanup(unet_patcher)

        return samples

    def sample(self, p, x, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        unet_patcher = self.model_wrap.inner_model.forge_objects.unet
        sampling_prepare(self.model_wrap.inner_model.forge_objects.unet, x=x)

        self.model_wrap.log_sigmas = self.model_wrap.log_sigmas.to(x.device)
        self.model_wrap.sigmas = self.model_wrap.sigmas.to(x.device)

        steps = steps or p.steps

        sigmas = self.get_sigmas(p, steps).to(x.device)

        if opts.sgm_noise_multiplier:
            p.extra_generation_params["SGM noise multiplier"] = True
            x = x * torch.sqrt(1.0 + sigmas[0] ** 2.0)
        else:
            x = x * sigmas[0]

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'n' in parameters:
            extra_params_kwargs['n'] = steps

        if 'sigma_min' in parameters:
            extra_params_kwargs['sigma_min'] = self.model_wrap.sigmas[0].item()
            extra_params_kwargs['sigma_max'] = self.model_wrap.sigmas[-1].item()

        if 'sigmas' in parameters:
            extra_params_kwargs['sigmas'] = sigmas

        if self.config.options.get('brownian_noise', False):
            noise_sampler = self.create_noise_sampler(x, sigmas, p)
            extra_params_kwargs['noise_sampler'] = noise_sampler

        if opts.sd_sampling == "A1111":
            if self.config.options.get('solver_type', None) == 'heun':
                extra_params_kwargs['solver_type'] = 'heun'
        else:
            pass

        self.last_latent = x
        self.sampler_extra_args = {
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }

        samples = self.launch_sampling(steps, lambda: self.func(self.model_wrap_cfg, x, extra_args=self.sampler_extra_args, disable=False, callback=self.callback_state, **extra_params_kwargs))

        self.add_infotext(p)

        sampling_cleanup(unet_patcher)

        return samples


