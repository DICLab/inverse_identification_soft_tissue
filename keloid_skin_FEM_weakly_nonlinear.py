'''
Solve the forward problem of keloid skin extension under displacement control.
Right pad fixed at zero displacement; left pad fixed for incremental displacement.
Determine the model parameters by minimizing the mismatch between the numerical
reaction force and target reaction force.

TODO:
* For verification purposes, convert observed displacement field to discrete
    data points, e.g. on a regular grid or random sampling points. It is too
    simplistic to export the measurements in a form of functions.

'''

from dolfin import *
import dolfin

from dolfin import (
    action,
    assemble,
    Function,
    Constant,
    Expression,
    DirichletBC,
    )

import sys
import math
import logging
import importlib
import numpy as np
import scipy.linalg as linalg
import matplotlib.pyplot as plt

import config # configure printing and logging options
import invsolve

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
dolfin.set_log_level(logging.WARNING)

logging.getLogger('UFL').setLevel(logging.WARNING)
logging.getLogger('FFC').setLevel(logging.WARNING)


### Solution settings

FIX_EXTERNAL_BOUNDARY = False
OBSERVE_LAST_MEASUREMENT = False
PLOT_DATA = True
ELEMENT_DEGREE = 1

## Fabricate measurements

# Left pad displacements (horizontal)
n_point = 101
x_a = 0
x_b = -4.0

ux_msr_pad_left = np.linspace(x_a,x_b, n_point)

### Get first mesh

import keloid_skin_mesh_reference
from keloid_skin_mesh_reference import (
    mesh_domain,
    markers_domain,
    markers_boundary,
    id_markers_domain,
    id_markers_boundary)

dx_material = [
    dolfin.Measure('dx',
        domain=mesh_domain,
        subdomain_data=markers_domain,
        subdomain_id=(
            id_markers_domain['keloid_measure'],
            )
        ),
    dolfin.Measure('dx',
        domain=mesh_domain,
        subdomain_data=markers_domain,
        subdomain_id=(
            id_markers_domain['healthy'],
            id_markers_domain['healthy_measure'],
            )
        ),
    ]

dx_measure = [
    dolfin.Measure('dx',
        domain=mesh_domain,
        subdomain_data=markers_domain,
        subdomain_id=(
            id_markers_domain['keloid_measure'],
            id_markers_domain['healthy_measure'],
            )
        ),
    ]

ds_boundary_pad_one = dolfin.Measure('ds',
    domain=mesh_domain,
    subdomain_data=markers_boundary,
    subdomain_id=(id_markers_boundary['pad_one_sensor'],))

ds_measure = [ds_boundary_pad_one]

dx_mat = dx_material
ds_pad = ds_measure[0]


### To check if the external pad is well identified.
ds_boundary_pad_one_external = dolfin.Measure('ds',
    domain=mesh_domain,
    subdomain_data=markers_boundary,
    subdomain_id=(id_markers_boundary['pad_one'],))

print('Sensor pad surface integration length', dolfin.assemble(1*ds_pad))
print('External pad perimeter', dolfin.assemble(1*ds_boundary_pad_one_external))


### Function spaces

V = dolfin.VectorFunctionSpace(mesh_domain, 'Lagrange', ELEMENT_DEGREE) # mesh, family, degree
V_R = dolfin.FunctionSpace(mesh_domain, 'Real', 0)

### Dirichlet boundary conditions

uD_msr_pad_left = Expression(('ux','uy'), ux=0.0, uy=0.0, degree=0)
uD_msr_pad_right = Expression(('ux','uy'), ux=0.0, uy=0.0, degree=0)

bcs = [DirichletBC(V, uD_msr_pad_left, markers_boundary, id_markers_boundary['pad_one']),
       DirichletBC(V, uD_msr_pad_left, markers_boundary, id_markers_boundary['pad_one_sensor']),
       DirichletBC(V, uD_msr_pad_right, markers_boundary, id_markers_boundary['pad_two'])
       ]


EPS_DOLFIN = 1e-14
def bottom_boundary(x, on_boundary):
	return on_boundary and near(x[1], -5, EPS_DOLFIN)

def top_boundary(x, on_boundary):
	return on_boundary and near(x[1], 45, EPS_DOLFIN)

def left_boundary(x, on_boundary):
	return on_boundary and near(x[0], 0, EPS_DOLFIN)

def right_boundary(x, on_boundary):
	return on_boundary and near(x[0], 100, EPS_DOLFIN)

if FIX_EXTERNAL_BOUNDARY:

    uD_x = Constant(0.0)
    uD_y = Constant(0.0)

    V_x, V_y = V.split()

    bcs.extend([
        DirichletBC(V_y, uD_y, bottom_boundary),
        DirichletBC(V_y, uD_y, top_boundary),
        DirichletBC(V_x, uD_x, left_boundary),
        DirichletBC(V_x, uD_x, right_boundary),
        ])


### Reference material

material_parameters = [
        {'mu': Constant(0.05),
         'jm': Constant(0.2)},
        {'mu': Constant(0.016),
         'jm': Constant(0.4)}]


### Measurement times

n_msr = len(ux_msr_pad_left)
t_msr = tuple(range(0,n_msr))

t_obs = t_msr

if OBSERVE_LAST_MEASUREMENT:
    logger.info('Assuming last measurement time.')
    t_obs = [t_msr[-1],]

def grad_reduc(X):
	# Transform the deformation gradient tenso 'F' to a 3D tensor
	e = grad(X)
	return as_tensor([[e[0, 0], e[0, 1], 0],
                      [e[1, 0], e[1, 1], 0],
                      [0, 0, 0]])

def dim_reduc(X):
	# Transform a 3D tensor to 2D
	return as_tensor([[X[0, 0], X[0, 1]],
                      [X[1, 0], X[1, 1]]])



### Define the Material Model

def Psi_(u, material_parameters):
    '''Strain energy density'''

    I = Identity(3)             # Identity tensor
    F = variable(I + grad_reduc(u))       # Deformation gradient
    # F = variable(I + grad(u))       # Deformation gradient
    C = F.T*F                   # Right Cauchy-Green tensor
    B = F*F.T   				# Left Cauchy-Green tensor
    # E = 0.5*(C-I)
    J = det(F)
    # Cbar = J**(-2.0/3.0)*C

    I1 = tr(C)
    I2 = 0.5*(tr(C)**2 - tr(C*C))
    I3 = det(C)
    IB = tr(B)


    mu = material_parameters['mu']
    jm = material_parameters['jm']

    psi = -0.5*mu*(jm*ln(1 - (IB - 3)/jm) + 2*ln(J)) # Gent compressible

    PK1 = diff(psi, F)
    PK2 = dot(inv(F), PK1)

    return psi, PK1, PK2

def Pi_(u, mp_sub, dx_sub): # f_msr, dx_msr_f):
    '''Potential energy

    Parameters
    ----------
    u : dolfin.Function
        The displacement field, a vector values function.
    mp_sub : iterable of dict's whose values are dolfin.Constant's
        Material parameters for each material subdomain.
    dx_sub: iterable of dolfin.Measure's
        Material integration subdomains.

    Returns
    -------
    Pi : ufl.Form
        The potential energy of the hyper-elastic solid

    '''

    W_int = W_ext = 0

    # deformation energy
    for mp_sub_i, dx_sub_i in zip(mp_sub, dx_sub):
        psi, *_ = Psi_(u, mp_sub_i)
        W_int += psi * dx_sub_i

    # # external load potential
    # for f_msr_i, dx_msr_i in zip(f_msr, dx_msr_f):
    #     W_ext += dot(u, f_msr_i) * dx_msr_i

    Pi = W_int - W_ext

    return Pi

u = Function(V)

Pi = Pi_(u, material_parameters, dx_material)

psi_keloid, P_keloid, S_keloid = Psi_(u, material_parameters[0])
psi_healthy, P_healthy, S_healthy = Psi_(u, material_parameters[1])

N = dolfin.FacetNormal(mesh_domain)

T = dolfin.dot(dim_reduc(P_keloid), N) # pad traction (force/surface)

Tx, Ty = T[0], T[1]

### Inverse solver

# NOTE: If the objective functional (cost) is an equality constraint,
# it will be necessary to pre-multiply it by a lagrange multiplier.

# NOTE: The number of constraint equations should be fewer than the
# number of model parameters.

lagrange_multipliers = [Constant(1e-6),] # initialize to a small value
# lagrange_multipliers = [Constant(2.117582368135751e-22)]

fx_target = Function(V_R)
fy_target = Function(V_R)

fx_target.vector().set_local([-1.0])
fy_target.vector().set_local([0.0])

Tx_target = fx_target / assemble(1*ds_pad) # average traction (force/surface)
Ty_target = fy_target / assemble(1*ds_pad) # average traction (force/surface)

cost = lagrange_multipliers[0] * (Tx - Tx_target) * ds_pad # activate/deactivate
# cost = lagrange_multipliers[0] * (Ty - Ty_target) * ds_pad # activate/deactivate
# cost += (material_parameters['nu'] - 0.3)**2 * dx_mat # stabilization
C_f = 0.0
J_u = 0.0

class model:
    u = u
    Pi = Pi
    bcs = bcs

model_parameters = [material_parameters, lagrange_multipliers] # to be optimized

# NOTE: Putting all Lagrange multipliers last in `model_parameters` to make
# easier indexing of the material parameters, which are of primary interset.

def measurement_setter(t):
    '''To be called inside `InverseSolver` for each solution time `t`.
    It is responcible for assigning measurement values to variables.'''
    uD_msr_pad_left.ux = ux_msr_pad_left[t] # set dirichlet BC at time


ip = invsolve.invsolve.InverseSolver(
    cost, model, model_parameters, J_u, C_f, measurement_setter, observation_times=t_obs)


u_msr = []

fx_msr_pad_left = []
Tx_ds = Tx * ds_pad

for i, t in enumerate(t_msr):

    ip.solve_nonlinear_problem(t)
    print(t+1, '/', t_msr[-1]+1, 'done')

    u_msr.append(u.copy(deepcopy=True))
    fx_msr_pad_left.append(dolfin.assemble(Tx_ds))

    file = File("results/generated_displacement/displacement_"+str(t_msr[t])+".pvd");
    file << u_msr[-1]


out = {
    'u_msr_dom_func': u_msr,
    'ux_msr_pad_left_vals': ux_msr_pad_left,  # Prescribed displacement on pads
    'fx_msr_pad_left_vals': fx_msr_pad_left,
    'Vector Function Space': V,
    'FEM domain': dx_material,
    }
