import ivy_module as im
import ivy_actions as ia
import ivy_logic as il
import ivy_transrel as tr
import ivy_logic_utils as ilu
import ivy_utils as iu
import ivy_art as art
import ivy_interp as itp
import ivy_theory as thy

import tempfile
import subprocess


def get_truth(digits,idx,syms):
    if (len(digits) != len(syms)):
        badwit()
    digit = digits[idx]
    if digit == '0':
        return il.Or()
    elif digit == '1':
        return il.And()
    elif digit != 'x':
        badwit()
    return None


class Aiger(object):
    def __init__(self,inputs,latches,outputs):
        iu.dbg('inputs')
        iu.dbg('latches')
        iu.dbg('outputs')
        inputs = inputs + [il.Symbol('%%bogus%%',il.find_sort('bool'))] # work around abc bug
        self.inputs = inputs
        self.latches = latches
        self.outputs = outputs
        self.gates = []
        self.map = dict()
        self.next_id = 1
        self.values = dict()
        for x in inputs + latches:
            self.map[x] = self.next_id * 2
            self.next_id += 1
        
    def true(self):
        return 1

    def false(self):
        return 0

    def lit(self,sym):
        return self.map[sym]

    def define(self,sym,val):
        self.map[sym] = val
        
    def andl(self,*args):
        if not args:
            return self.true()
        res = args[0]
        for x in args[1:]:
            tmp = self.next_id * 2
            self.gates.append((tmp,res,x))
            self.next_id += 1
            res = tmp
        return res

    def notl(self,arg):
        return 2*(arg/2) + (1 - arg%2)
    
    def orl(self,*args):
        return self.notl(self.andl(*map(self.notl,args)))

    def ite(self,x,y,z):
        return self.orl(self.andl(x,y),self.andl(self.notl(x),z))

    def iff(self,x,y):
        return self.orl(self.andl(x,y),self.andl(self.notl(x),self.notl(y)))

    def xor(self,x,y):
        return self.notl(self.iff(x,y))

    def eval(self,expr,getdef=None):
        def recur(expr):
            if il.is_app(expr):
                sym = expr.rep
                assert il.is_boolean_sort(sym.sort),"non-boolean sym in aiger output: {}".format(sym)
                try:
                    return self.lit(sym)
                except KeyError:
                    assert getdef is not None, "no definition for {} in aiger output".format(sym)
                    return getdef(sym)
            else:
                args = map(recur,expr.args)
                if isinstance(expr,il.And):
                    return self.andl(*args)
                if isinstance(expr,il.Or):
                    return self.orl(*args)
                if isinstance(expr,il.Not):
                    return self.notl(*args)
                assert False,"non-boolean op in aiger output: {}".format(type(expr))
        return recur(expr)


    def deflist(self,defs):
        dmap = dict((df.defines(),df.args[1]) for df in defs)
        def getdef(sym):
            if sym not in dmap:
                assert getdef is not None, "no definition for {} in aiger output".format(sym)
            val = self.eval(dmap[sym],getdef)
            self.define(sym,val)
            return val
        for df in defs:
            sym = df.defines()
            assert il.is_boolean_sort(sym.sort),"non-boolean sym in aiger output: {}".format(sym)
            self.define(sym,self.eval(df.args[1],getdef))
    
    def set(self,sym,val):
        self.values[sym] = val

    def get_state(self,post):
        res = dict()
        for i,v in enumerate(self.latches):
            res[v] = get_truth(post,i,self.latches)
        return res

    def __str__(self):
        strings = []
        strings.append('aag {} {} {} {} {}'.format(self.next_id - 1,len(self.inputs),
                                                   len(self.latches),len(self.outputs),
                                                   self.next_id - 1 - (len(self.inputs) + len(self.latches))))
        for x in self.inputs:
            strings.append(str(self.map[x]))
        for x in self.latches:
            strings.append(str('{} {}'.format(self.map[x],self.values[x])))
        for x in self.outputs:
            strings.append(str(self.values[x]))
        for x,y,z in self.gates:
            strings.append(str('{} {} {}'.format(x,y,z)))
        return '\n'.join(strings)+'\n'
                                          
# functions for binary encoding of finite sorts

def ceillog2(n):
    bits,vals = 0,1
    while vals < n:
        bits += 1
        vals *= 2
    return bits

def get_encoding_bits(sort):
    iu.dbg('sort')
    interp = thy.get_sort_theory(sort)
    if il.is_enumerated_sort(interp):
        n = ceillog2(len(interp.defines()))
    elif isinstance(interp,thy.BitVectorTheory):
        n = interp.bits
    elif il.is_boolean_sort(interp):
        n = 1
    else:
        msg = 'model checking cannot handle sort {}'.format(sort)
        if interp is not sort:
            msg += '(interpreted as {})'.format(interp)
        raise iu.IvyError(None,msg)
    return n
                                  

def encode_vars(syms,encoding):
    res = []
    for sym in syms:
        n = get_encoding_bits(sym.sort)
        vs = [sym.suffix('[{}]'.format(i)) for i in range(n)]
        encoding[sym] = vs
        res.extend(vs)
    return res

class Encoder(object):
    def __init__(self,inputs,latches,outputs):
        iu.dbg('inputs')
        iu.dbg('latches')
        iu.dbg('outputs')
        self.inputs = inputs
        self.latches = latches
        self.outputs = outputs
        self.encoding = dict()
        self.pos = dict()
        subinputs = encode_vars(inputs,self.encoding)
        sublatches = encode_vars(latches,self.encoding)
        suboutputs = encode_vars(outputs,self.encoding)
        self.sub = Aiger(subinputs,sublatches,suboutputs)
        self.ops = {
            '+' : self.encode_plus,
            '-' : self.encode_minus,
            '*' : self.encode_times,
            '/' : self.encode_div,
            '%' : self.encode_mod,
            '<' : self.encode_le,
            }
        
    def true(self):
        return [self.sub.true()]

    def false(self):
        return [self.sub.false()]

    def lit(self,sym):
        return map(self.sub.lit,self.encoding[sym])

    def define(self,sym,val):
        vs = encode_vars([sym],self.encoding)
        for s,v in zip(vs,val):
            self.sub.define(s,v )
        
    def andl(self,*args):
        if len(args) == 0:
            return self.true()
        return [self.sub.andl(*v) for v in zip(*args)]

    def orl(self,*args):
        if len(args) == 0:
            return self.false()
        return [self.sub.orl(*v) for v in zip(*args)]

    def notl(self,arg):
        return map(self.sub.notl,arg)
    

    def eval(self,expr,getdef=None):
        def recur(expr):
            if isinstance(expr,il.Ite):
                cond = recur(expr.args[0])
                thenterm = recur(expr.args[1])
                elseterm = recur(expr.args[2])
                res = [self.sub.ite(cond[0],x,y) for x,y in zip(thenterm,elseterm)]
            elif il.is_app(expr):
                sym = expr.rep 
                if sym in il.sig.constructors:
                    m = sym.sort.defines().index(sym.name)
                    res = self.binenc(m,ceillog2(len(sym.sort.defines())))
                elif sym.is_numeral() and il.is_interpreted_sort(sym.sort):
                    n = get_encoding_bits(sym.sort)
                    res = self.binenc(int(sym.name),n)
                elif sym.name in self.ops and il.is_interpreted_sort(sym.sort.dom[0]):
                    args = map(recur,expr.args)
                    res = self.ops[sym.name](expr.args[0].sort,*args)
                else:
                    assert len(expr.args) == 0
                    try:
                        res = self.lit(sym)
                    except KeyError:
                        assert getdef is not None, "no definition for {} in aiger output".format(sym)
                        res = getdef(sym)
            else:
                args = map(recur,expr.args)
                if isinstance(expr,il.And):
                    res = self.andl(*args)
                elif isinstance(expr,il.Or):
                    res = self.orl(*args)
                elif isinstance(expr,il.Not):
                    res = self.notl(*args)
                elif il.is_eq(expr):
                    res = self.encode_equality(expr.args[0].sort,*args)
                else:
                    assert False,"unimplemented op in aiger output: {}".format(type(expr))
            iu.dbg('expr')
            iu.dbg('res')
            return res
        res = recur(expr)
        assert len(res) > 0
        return res

    def deflist(self,defs):
        dmap = dict((df.defines(),df.args[1]) for df in defs)
        def getdef(sym):
            if sym not in dmap:
                assert getdef is not None, "no definition for {} in aiger output".format(sym)
            val = self.eval(dmap[sym],getdef)
            self.define(sym,val)
            return val
        for df in defs:
            sym = df.defines()
            self.define(sym,self.eval(df.args[1],getdef))
    
    def set(self,sym,val):
        iu.dbg('sym')
        iu.dbg('val')
        assert len(val) > 0
        for x,y in zip(self.encoding[sym],val):
            iu.dbg('x')
            iu.dbg('y')
            self.sub.set(x,y)

    def __str__(self):
        return str(self.sub)

    def gebin(self,bits,n):
        iu.dbg('bits')
        iu.dbg('n')
        if n == 0:
            return self.sub.true()
        if n >= 2**len(bits):
            return self.sub.false()
        hval = 2**(len(bits)-1)
        if hval <= n:
            return self.sub.andl(bits[0],self.gebin(bits[1:],n-hval))
        return self.sub.orl(bits[0],self.gebin(bits[1:],n))

    def binenc(self,m,n):
        return [(self.sub.true() if m & (1 << (n-1-i)) else self.sub.false())
                for i in range(n)]
        
    def bindec(self,bits):
        res = 0
        n = len(bits)
        for i,v in enumerate(bits):
            if isinstance(v,il.And):
                res += 1 << (n - 1 - i)
        return res

    def encode_equality(self,sort,*eterms):
        n = len(sort.defines()) if il.is_enumerated_sort(sort) else 2**len(eterms[0])
        bits = ceillog2(n)
        iu.dbg('eterms')
        eqs = self.sub.andl(*[self.sub.iff(x,y) for x,y in zip(*eterms)])
        alt = self.sub.andl(*[self.gebin(e,n-1) for e in eterms])
        res =  [self.sub.orl(eqs,alt)]
        return res

    def encode_plus(self,sort,x,y,cy=None):
        res = []
        if cy is None:
            cy = self.sub.false()
        for i in range(len(x)-1,-1,-1):
            res.append(self.sub.xor(self.sub.xor(x[i],y[i]),cy))
            cy = self.sub.orl(self.sub.andl(x[i],y[i]),self.sub.andl(x[i],cy),self.sub.andl(y[i],cy))
        res.reverse()
        return res

    def encode_minus(self,sort,x,y):
        ycom = self.notl(y)
        return self.encode_plus(sort,x,ycom,self.sub.true())

    def encode_times(self,sort,x,y):
        res = [self.sub.false() for _ in x]
        for i in range(0,len(x)):
            res = res[1:] + [self.sub.false()]
            res = self.encode_ite(sort,x[i],self.encode_plus(sort,res,y),res)
        return res

    def encode_lt(self,sort,x,y,cy=None):
        if cy is None:
            cy = self.sub.false()
        for i in range(len(x)-1,-1,-1):
            cy = self.sub.orl(self.sub.andl(x[i],y[i]),self.sub.andl(self.sub.iff(x[i],y[i]),cy))
        return cy
            
    def encode_le(self,sort,x,y):
        return encode_lt(self,sort,x,y,cy=self.sub.true())

    def encode_div(self,sort,x,y):
        thing = [self.sub.false() for _ in x]
        res = []
        for i in range(0,len(x)):
            thing = thing[1:] + [x[i]]
            le = encode_le(y,thing)
            thing = self.encode_ite(sort,ls,self.encode_minus(sort,thing,y),thing)
            res.append(le)
        return res

    def encode_mod(self,sort,x,y):
        return self.encode_sub(x,self.encode_div(x,y))

    def get_state(self,post):
        subres = self.sub.get_state(post)
        res = dict()
        for v in self.latches:
            bits = [subres[s] for s in self.encoding[v]]
            interp = thy.get_sort_theory(v.sort)
            if il.is_enumerated_sort(interp):
                num = self.bindec(bits)
                vals = v.sort.defines()
                val = vals[num] if num < len(vals) else vals[-1]
                val = il.Symbol(val,v.sort)
            elif isinstance(interp,thy.BitVectorTheory):
                num = self.bindec(bits)
                val = il.Symbol(str(num),v.sort)
            elif il.is_boolean_sort(interp):
                val = bits[0]
            else:
                assert False,'variable has unexpected sort: {} {}'.format(v,s.sort)
            res[v] = val
        return res

def is_finite_sort(sort):
    interp = thy.get_sort_theory(sort)
    return (il.is_enumerated_sort(interp) or 
            isinstance(interp,thy.BitVectorTheory) or
            il.is_boolean_sort(interp))

# Tricky: if an atomic proposition has a next variable in it, but no curremnt versions of state
# variables, it is a candidate to become an abstract state variable. THis function computes the
# current state version of such an express from its next state version, or returns None if the expression
# does not qualify.
# 

def prev_expr(stvarset,expr):
    if any(sym in stvarset for sym in ilu.symbols_ast(expr)):
        return None
    news = [sym for sym in ilu.used_symbols_ast(expr) if tr.is_new(sym)]
    if news:
        rn = dict((sym,tr.new_of(sym)) for sym in news)
        return ilu.rename_ast(expr,rn)
    return None        

def to_aiger(mod,ext_act):

    # we use a special state variable __init to indicate the initial state

    ext_acts = [mod.actions[x] for x in sorted(mod.public_actions)]
    ext_act = ia.EnvAction(*ext_acts)
    init_var = il.Symbol('__init',il.find_sort('bool')) 
    init = ia.Sequence(*([a for n,a in mod.initializers]+[ia.AssignAction(init_var,il.And())]))
    action = ia.IfAction(init_var,ext_act,init)

    # get the invariant to be proved:

    invariant = il.And(*[lf.formula for lf in mod.labeled_conjs])

    # compute the transition relation

    stvars,trans,error = action.update(mod,None)
    
    # get the background theory

    axioms = mod.background_theory()
#    iu.dbg('axioms')

    # Propositionally abstract

    stvarset = set(stvars)
    prop_abs = dict()
    global prop_abs_ctr  # sigh -- python lameness
    prop_abs_ctr = 0
    new_stvars = []
    def new_prop(expr):
        res = prop_abs.get(expr,None)
        if res is None:
            prev = prev_expr(stvarset,expr)
            if prev is not None:
                pva = new_prop(prev)
                res = tr.new(pva)
                new_stvars.append(pva)
            else:
                global prop_abs_ctr
                res = il.Symbol('__abs[{}]'.format(prop_abs_ctr),expr.sort)
                prop_abs[expr] = res
                prop_abs_ctr += 1
        return res
    def mk_prop_abs(expr):
        if (il.is_quantifier(expr) or 
            len(expr.args) > 0 and any(not is_finite_sort(a.sort) for a in expr.args)):
            return new_prop(expr)
        return expr.clone(map(mk_prop_abs,expr.args))
    new_defs = []
    for df in trans.defs:
        if len(df.args[0].args) == 0 and is_finite_sort(df.args[0].sort):
            new_defs.append(df)
        else:
            prop_abs[df.to_constraint()] = il.And()
    new_defs = map(mk_prop_abs,new_defs)
    new_fmlas = [mk_prop_abs(il.close_formula(fmla)) for fmla in trans.fmlas]
    trans = ilu.Clauses(new_fmlas,new_defs)
    invariant = mk_prop_abs(invariant)
    rn = dict((sym,tr.new(sym)) for sym in stvars)
    mk_prop_abs(ilu.rename_ast(invariant,rn))  # this is to pick up state variables from invariant
    stvars = [sym for sym in stvars if is_finite_sort(sym.sort)] + new_stvars

    iu.dbg('trans')
    iu.dbg('stvars')
    iu.dbg('invariant')
    exit(0)

    # For each state var, create a variable that corresponds to the input of its latch

    def fix(v):
        return v.prefix('nondet')
    stvars_fix_map = dict((tr.new(v),fix(v)) for v in stvars)
    trans = ilu.rename_clauses(trans,stvars_fix_map)
    iu.dbg('trans')
    new_defs = trans.defs + [il.Definition(ilu.sym_inst(tr.new(v)),ilu.sym_inst(fix(v))) for v in stvars]
    trans = ilu.Clauses(trans.fmlas,new_defs)
    
    # Turn the transition constraint into a definition
    
    cnst_var = il.Symbol('__cnst',il.find_sort('bool'))
    new_defs = trans.defs + [il.Definition(tr.new(cnst_var),il.Not(il.And(*trans.fmlas)))]
    stvars.append(cnst_var)
    trans = ilu.Clauses([],new_defs)
    
    # Input are all the non-defined symbols. Output indicates invariant is false.

    iu.dbg('trans')
    def_set = set(df.defines() for df in trans.defs)
    def_set.update(stvars)
    iu.dbg('def_set')
    inputs = [sym for sym in ilu.used_symbols_clauses(trans) if
              sym not in def_set and not il.is_interpreted_symbol(sym)]
    fail = il.Symbol('__fail',il.find_sort('bool'))
    outputs = [fail]
    
    # make an aiger

    aiger = Encoder(inputs,stvars,outputs)
    comb_defs = [df for df in trans.defs if not tr.is_new(df.defines())]
    aiger.deflist(comb_defs)
    for df in trans.defs:
        if tr.is_new(df.defines()):
            aiger.set(tr.new_of(df.defines()),aiger.eval(df.args[1]))
    aiger.set(fail,aiger.eval(il.And(init_var,il.Not(cnst_var),il.Not(invariant))))

    return aiger

def badwit():
    raise iu.IvyError(None,'model checker returned mis-formated witness')

# This is an adaptor to create a trace as an ART. 

class IvyMCTrace(art.AnalysisGraph):
    def __init__(self,stvals):
        iu.dbg('stvals')
        def abstractor(state):
            state.clauses = ilu.Clauses(stvals)
            state.universes = dict() # indicates this is a singleton state
        art.AnalysisGraph.__init__(self,initializer=abstractor)
    def add_state(self,stvals,action):
        iu.dbg('stvals')
        self.add(itp.State(value=ilu.Clauses(stvals),expr=itp.action_app(action,self.states[-1]),label='ext'))

def aiger_witness_to_ivy_trace(aiger,witnessfilename,ext_act):
    with open(witnessfilename,'r') as f:
        res = f.readline().strip()
        iu.dbg('res')
        if res != '1':
            badwit()
        tr = None
        for line in f:
            if line.endswith('\n'):
                line = line[:-1]
            cols = line.split(' ')
            iu.dbg('cols')
            if len(cols) != 4:
                badwit()
            pre,inp,out,post = cols
            stvals = []
            stmap = aiger.get_state(post)                     
            iu.dbg('stmap')
            for v in aiger.latches[:-2]: # last two are used for encoding
                val = stmap[v]
                if val is not None:
                    stvals.append(il.Equals(v,val))
            if not tr:
                tr = IvyMCTrace(stvals) # first transition is initialization
            else:
                tr.add_state(stvals,ext_act) # remainder are exported actions
        if tr is None:
            badwit()
        return tr

class ModelChecker(object):
    pass

class ABCModelChecker(ModelChecker):
    def cmd(self,aigfilename,outfilename):
        return ['abc','-c','read_aiger {}; pdr; write_aiger_cex  {}'.format(aigfilename,outfilename)]
    def scrape(self,alltext):
        return 'Property proved' in alltext


def check_isolate():
    
    mod = im.module

    # build up a single action that does both initialization and all external actions

    ext_acts = [mod.actions[x] for x in sorted(mod.public_actions)]
    ext_act = ia.EnvAction(*ext_acts)
    
    # convert to aiger

    aiger = to_aiger(mod,ext_act)
    print aiger

    # output aiger to temp file

    with tempfile.NamedTemporaryFile(suffix='.aag',delete=False) as f:
        name = f.name
        print 'file name: {}'.format(name)
        f.write(str(aiger))
    
    # convert aag to aig format

    aigfilename = name.replace('.aag','.aig')
    try:
        ret = subprocess.call(['aigtoaig',name,aigfilename])
    except:
        raise iu.IvyError(None,'failed to run aigtoaig')
    if ret != 0:
        raise iu.IvyError(None,'aigtoaig returned non-zero status')
        
    # run model checker

    outfilename = name.replace('.aag','.out')
    mc = ABCModelChecker() # TODO: make a command-line option
    cmd = mc.cmd(aigfilename,outfilename)
    print cmd
    try:
        p = subprocess.Popen(cmd,stdout=subprocess.PIPE)
    except:
        raise iu.IvyError(None,'failed to run model checker')

    # pass through the stdout and collect it in texts

    texts = []
    while True:
        text = p.stdout.read(256)
        print text,
        texts.append(text)
        if len(text) < 256:
            break
    alltext = ''.join(texts)
    
    # get the model checker status

    ret = p.wait()
    if ret != 0:
        raise iu.IvyError(None,'model checker returned non-zero status')

    # scrape the output to get the answer

    if mc.scrape(alltext):
        print 'PASS'
    else:
        print 'FAIL'
        tr = aiger_witness_to_ivy_trace(aiger,outfilename,ext_act)        
        import tk_ui as ui
        iu.set_parameters({'mode':'induction'})
        gui = ui.new_ui()
        agui = gui.add(tr)
        gui.tk.update_idletasks() # so that dialog is on top of main window
        gui.tk.mainloop()
        exit(1)

        
    exit(0)


    
