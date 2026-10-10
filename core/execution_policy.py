"""Frozen resource choices. Profiles change work partitioning, never models.

Missing policy means exact historical defaults. MDX time dimensions are model
shapes, so its profile controls CPU threads instead of inventing segment sizes.
"""
import copy

DEVICES = ('auto', 'cpu', 'cuda')
PROFILES = {
    'compatibility': {'cpu_threads': 1, 'hp2_window': 384, 'demucs_segment': 3,
                      'rvc': {'x-pad': 1, 'x-query': 2, 'x-center': 12, 'x-max': 18}},
    'balanced': {'cpu_threads': 2, 'hp2_window': 512, 'demucs_segment': 6,
                 'rvc': {'x-pad': 1, 'x-query': 5, 'x-center': 25, 'x-max': 30}},
    'quality': {'cpu_threads': 4, 'hp2_window': 768, 'demucs_segment': 7.5,
                'rvc': {'x-pad': 1, 'x-query': 6, 'x-center': 30, 'x-max': 40}},
}


def freeze_execution_policy(device='auto', performance_profile='balanced'):
    if not isinstance(device, str) or not isinstance(performance_profile, str) or device not in DEVICES or performance_profile not in PROFILES:
        raise ValueError('设备或性能档无效')
    return {'version': 1, 'device': device, 'performance_profile': performance_profile,
            'hp2_cudnn': True,
            'hp2_zero_initialized_bands': True,
            **copy.deepcopy(PROFILES[performance_profile])}


def validate_execution_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError('execution_policy 必须是冻结对象')
    expected = freeze_execution_policy(policy.get('device'), policy.get('performance_profile'))
    if 'hp2_cudnn' not in policy:
        # Earlier frozen v1 policies retain their original convolution path.
        expected.pop('hp2_cudnn')
    elif type(policy['hp2_cudnn']) is bool:
        expected['hp2_cudnn'] = policy['hp2_cudnn']
    if 'hp2_zero_initialized_bands' not in policy:
        expected.pop('hp2_zero_initialized_bands')
    def same_types(actual, template):
        if type(actual) is not type(template):
            return False
        if isinstance(template, dict):
            return actual.keys() == template.keys() and all(same_types(actual[key], value) for key, value in template.items())
        return actual == template
    if not same_types(policy, expected):
        raise ValueError('execution_policy 与冻结档位不一致')
    return copy.deepcopy(policy)


def worker_policy(policy):
    return None if policy is None else validate_execution_policy(policy)
