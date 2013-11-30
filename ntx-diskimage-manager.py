#! python3
# -*- coding: UTF-8 -*-

import sys
import platform
import os.path
#import subprocess
from pprint import *

import argparse
import configparser
import json
import mmap
import contextlib
from collections import defaultdict
import struct

class Error(Exception): pass

def load_json(filepath):
	with open(filepath, encoding = "utf-8") as f:
		json_data = json.load(f)
	return json_data

###############

#[dd template出力時のみ意味があるオプション]
#unix ddの場合は"conv=notrunc"がいるが、これを指定できるようにする
#(ini側で書いてもいいのだが…)
#ddファイルへのパスを指定できるようにする
#[windows上でiniを書いたときのみ意味がある]
#iniのencodingを簡単に指定できるようにしたい

def create_commandline_parser():
	parser = argparse.ArgumentParser()
	parser.add_argument('-f', '--diskmap', help="diskmap spec file", action="store")
	parser.add_argument('-d', '--dd', help="dd program filepath", action="store")
	parser.add_argument('-t', '--dd-template', help="outut dd template", action="store_true")
	parser.add_argument('--debug', help="enable debug mode", action="store_true")
	parser.add_argument('--encoding', help="encoding for config file", action="store")
	parser.add_argument('config_file', metavar="config-file")
	platform_name = platform.system()
	if platform_name == 'Windows':
		#platform毎のデフォルト値変更は
		#parser.set_defaults(foo='spam')
		#固定デフォルトはadd_argument内でdefault='bar'等で指定する
		pass
	return parser


args = create_commandline_parser().parse_args()

#''だとデフォルトencodingになる
#このデフォルトはparserのadd_argument()で指定してしまうほうがいい
encoding = args.encoding if 'encoding' in args else ''

###############
#TODO: raw,ntxbin, u-bootのときのbinfile側のskip
#このうち、raw,ntxbinはjob/task側でoffset調整済
#ただし、readの時はちょっと困る

#read/write以外は特殊なことをせずに
#呼出し側にまかせて、ここではraw r/wに徹する

#本来ddでのr/wではbs指定してるのがnickelのupgrade処理だが、
#その辺をどうするか？byte offsetやbyte countでいいのかなあ？

#DD
#bs=BYTES はibs=obs=BYTESだが、オプションによっては違う？
#  あくまで読み書き単位の指定
#   oracleのmanだと
#     http://docs.oracle.com/cd/E19253-01/819-1211/6n3j8346v/index.html
#     sync、noerror、notrunc 以外の変換が 1 つも指定されない場合、
#     各入力ブロックは複数の短いブロックを 1 つにまとめる処理は行われず、
#     それぞれ単独のブロックとして出力側にコピーされます。
#   つまり、変換で内部バッファ(cbsサイズ)を経由しない場合は
#   block size packingが行われないということ？
#cbsは一度に変換するバイト数指定
#  ascii等の変換指定時のみ使われるもの(変換用バッファのサイズ)
#count=BLOCKS 入力側基準でBLOCKS個を処理する
#seekは出力側のseek block count
#skipは入力側のskip block count
#convのキーワード
#  notruncは出力ファイルをtruncしない指定
#  noerrorは入力エラーがあっても継続する(入力のみ？)
#  syncは全ての入力ブロックがibsで指定された
#   ブロックサイズとなるようにNULで埋める。 
#   入力側がibsになるようにpaddingする
#  noerrorで入力エラーになったときにzero fillするにはnoerror,sync
#  またnoerrorのときはcount指定必須らしい(eofで止まらない)
# ファイル相手だとbsはどうでもいいが、
# deviceの場合はsector size alignmentが必要
# ファイルの場合もbs=1だと遅いので本来は適切なサイズにしないといけない
#
#Koboのupdate
#[u-boot]
#	dd if=$UBOOT of=/dev/$DEVICE bs=1K seek=1 skip=1
#bs=1Kにして1skip1seekで書き込んでいる
#[kernel]
#	dd if=$KERNEL of=/dev/$DEVICE bs=512 seek=2048
#[waveform]
#	dd if=$WAVEFORM.header of=/dev/$DEVICE bs=512 seek=14335
#	dd if=$WAVEFORM of=/dev/$DEVICE bs=512 seek=14336

#定義済みpart
#raw指定
#  optionなし
#   各partデフォのoffset位置に、partファイル全体を書き込む
#  option指定
#   skip,countは指定可能(入力側partの部分指定)
#  * ubootの場合、update.zipの中のイメージはskip=1024が必要
#    イメージから抜いたuboot vs update.zip内のもの、両方を考える
#  * readの場合はcount指定必須
#ntxbin指定(file=header+data形式を想定)
#  optionなし
#   各partデフォのoffset位置にntxbin補正し、partファイル全体を書き込む
#  option指定
#   skip,countは指定可能(入力側partの部分指定)
#  * readの場合はcount指定必須
#
#raw part(未定義のパーツ)
#  raw=raw offset=xxx,rawfile.bin
#    offset指定必須
#    skipはoption
#    countもoption
#    readの場合はcount必須

#用語の定義
#I/Fとしてはバイトで全部指定する
# count:バイトサイズ(ddでのbs*count)
# offset:イメージファイルのバイトオフセット
#    (of=イメージならseek ifの場合はskip)
# skip:パーツ側のオフセット
#    (of=イメージならskip ifの場合はseek)
# ddを使う場合とは異なりr/wどちらでもパーツファイル側がskip固定
# 個数単位とskipが混乱要因

#conv=notruncが必要ないのはwindowsの特定のddのときだけだが、
#extractモード(dd reader)の場合、出力ファイルはtruncでいい

#tupleになっているものは(disk,part)順
#  (access, if_ofの呼出し引数)
#  あまりよろしくないのでdisk,part別に集約する変更予定
class Dd_spec:
	Reader = {
		'command': 'read',
		'mode': ('rb','w+b'),	#part側'r+b'ではcreateしない
		'access': (mmap.ACCESS_READ, mmap.ACCESS_WRITE),
		'if_of': lambda disk,part: (disk,part),
		'offset_to': "skip"
	}

	Writer = {
		'command': 'write',
		'mode': ('r+b','rb'),
		'access': (mmap.ACCESS_WRITE, mmap.ACCESS_READ),
		'if_of': lambda disk,part: (part,disk),	#swap
		'offset_to': "seek"
	}

#どうもデバッグ時はmmap.ACCESS_COPYにすれば
#メモリには書くがファイルには影響しない模様
if args.debug:
	Dd_spec.Reader["access"] = (Dd_spec.Reader["access"][0], mmap.ACCESS_COPY)
	Dd_spec.Writer["access"] = (mmap.ACCESS_COPY, Dd_spec.Writer["access"][1])


#accessの指定でflag,protが適切に指定される
#また、protを指定することでaccess=ACCESS_DEFAULTが適切な値になる
#(access),(prot,flag)のどちらか一方のみを指定する
#if platform.system() == 'Windows':
#	mmap.mmap(fileno, length,
#		tagname=None,
#		access=ACCESS_DEFAULT[, offset])
#else:
#	mmap.mmap(fileno, length,
#		flags=MAP_SHARED, prot=PROT_WRITE|PROT_READ,
#		access=ACCESS_DEFAULT[, offset])
#つまり、accessの指定だけでwindows,unixは同じとしていい
#debug purpose enum { ACCESS_DEFAULT,read,write,copy}
class Dd_base(Dd_spec):
	DEFAULT_BLOCKSIZE = 512
	DD = "dd"

	OptionFileKeywords = ("if", "of")
	#本当はibs,obsとかあるが使わないだろうから省略
	OptionBSKeywords = ("bs",)	#single element tuple
	OptionBSAffectedKeywords = ("skip", "seek", "count")
	OptionOtherKeywords = OptionBSKeywords + OptionBSAffectedKeywords
	OptionKeywords = OptionFileKeywords + OptionOtherKeywords
	OptionKeywordsOrder = defaultdict(lambda x: len(OptionKeywords),
		((v,i) for i,v in enumerate(OptionKeywords)))

	#paramsはattrで全てのkeywordが存在すること
	#(__getattr__等で未定義はNoneを返すように実装する)
	def generate_dd_param(self, file, params):
		dd_params = dict(zip(self.OptionFileKeywords,
			self.if_of(file, params.file)))
		for k in self.OptionOtherKeywords:
			dd_params[k] = getattr(params, k)
		#params.offsetは常にある
		#offsetをr/w適切にseek,skipにmappingする
		if dd_params[self.offset_to] is None:
			dd_params[self.offset_to] = params.offset
		return dd_params

	#順序はsorted()をかけることで適当にstableな結果にする
	#OptionKeywordsOrderに並べた順
	#paramからitem取り出しtuple→filer !None
	#→sort by order→string list→single string
	def generate_dd_param_string(self, params):
		return " ".join("{0[0]}={0[1]}".format(t) for t in
			sorted(
				((k,v) for k,v in params.items() if v is not None)
				, key = (lambda x: self.OptionKeywordsOrder[x[0]])
			)
		)

	#bsが影響するkeywordに関して
	#これらがalignment整合しているかをチェックして
	#byte to block変換する
	#u-boot等特殊なことをKobo公式システムで設定しているものは
	#それに準拠するよう、bs=1024等iniファイル側で設定する
	#(機能的にはあまり変わらないので準拠を気にしないなら不要)
	def align_blocksize(self, params):
		bs = self.OptionBSKeywords
		if isinstance(bs, tuple):
			bs = bs[0]
		blocksize = params[bs]
		if blocksize is None:
			blocksize = self.DEFAULT_BLOCKSIZE
		else:
			#blocksize自体512byte alignedでないといけない
			if (blocksize % self.DEFAULT_BLOCKSIZE) != 0:
				raise ValueError("blocksize={0} is not aligned by 512".format(blocksize))
		params[bs] = blocksize
		for k in self.OptionBSAffectedKeywords:
			if params[k] is not None:
				(v, rem) = divmod(params[k], blocksize)
				orig_size = params[k]
				params[k] = int(params[k]  / blocksize)
				if rem != 0:
					params[k] += 1
					print("{0}={1} is not aligned by {2}".format(k, orig_size, blocksize))
		return params

	def print_dd_template(self, filepath, params):
		ddp = self.generate_dd_param(filepath, params)
		print(" ".join((self.DD, self.generate_dd_param_string(self.align_blocksize(ddp)))))


class Dd_mmap(Dd_base):
	def __init__(self, file, spec = Dd_spec.Reader):
		super(Dd_mmap, self).__init__()
		self.__dict__.update(spec)
		self.filepath = file
		self.command = getattr(self, self.command)
		#
		self.file = open(file, self.mode[0])
		self.filesize = os.fstat(self.file.fileno()).st_size
		self.mem_disk = mmap.mmap(self.file.fileno(), self.filesize
			, access = self.access[0])

	def close(self):
		if not args.debug:
			self.mem_disk.close()
			self.file.close()

	def raw_read(self, offset, count):
		return self.mem_disk[offset:offset+count]

	def raw_write(self, offset, count, bytedata):
		self.mem_disk[offset:offset+count] = bytedata

	#kwargs部分をdictにしてformatを楽にしたほうがいいのかもしれない
	#pythonのmmapはplatform毎に異なる
	#mmapしたハンドルに対するm.read等はあくまでmapした領域に対する概念的file pointer
	#(mmap自体がfile like objectとして設計されている)
	#readはmap->他memory->writeとなりread段階でmemory copyが発生している
	#sliceの場合も結局bytesとして別領域を確保している。
	#完全にmap上でのview扱いで該当領域をdirectにwriteに回せないようだ。
	#このあたりはソースで確認済
	#sq_xxxxでシーケンス型のinterfaceはある
	#バイトにしてしまえばmemoryviewが使えるが、それ自体がbytearrayではない？
	#と思ったがmmap_as_mapping,mmap_as_bufferがあるので、
	#mapping, buffer protocolで使えるっぽい
	#mappingの場合はsqと同じような感じ
	#ただし、assignの場合は相手バッファをとってそこにcopyすることになるので
	#mmap to mmapだと速いかもしれない
	#しかし、
	#http://demianbrecht.github.io/posts/2013/02/10/buffer-and-memoryview/#memoryview-in-3-4
	#によると、3.4ではmmapでもmemoryviewがOkっぽい?2.7での話だけ？
	#buffer interfaceを使ってmemoryviewは実装されているはず
	#3.4でそのあたり新しい仕様になるようなことをみかけた記憶もあるが…
	def read(self, params):
		self.print_dd_template(self.filepath, params)
		#mmap操作自体はparamsを対象に行うこととする
		with open(params.file, self.mode[1]) as f:
			#ここはちょっとwriterとは処理が異なる
			#count指定されたサイズでmmapすることでfilesizeをcountに作り直す
			#これはrawならサイズ指定がいるしntxbinなら解析がいるので
			#callbackにするしかない
			#count指定があるときのntxbin補正は呼出し側で行っている
			#問題になるのはcountがなくてntxbin headerを解析しないといけないとき
			size = params.count #if isintance(params.count, int) else params.count(???)
			with contextlib.closing(mmap.mmap(f.fileno(), size
				, access = self.access[1])) as mem_part:
				with memoryview(self.mem_disk) as view_disk \
					, memoryview(mem_part) as view_part:
					view_part[:] = view_disk[params.offset:params.offset+size]

	#少なくともubootにskipがあるのでそれ対応がいる
	#skip+countが実sizeを超えないかチェック
	#seek+countが実サイズを超えないかチェック
	def write(self, params):
		self.print_dd_template(self.filepath, params)
		#mmap操作自体はparamsを対象に行うこととする
		with open(params.file, self.mode[1]) as f:
			size = os.fstat(f.fileno()).st_size
			with contextlib.closing(mmap.mmap(f.fileno(), size
				, access = self.access[1])) as mem_part:
				with memoryview(self.mem_disk) as view_disk \
					, memoryview(mem_part) as view_part:
					view_disk[params.offset:params.offset+size] = view_part[:]

class Dd_template(Dd_base):
	def __init__(self, file, spec = Dd_spec.Reader):
		super(Dd_template, self).__init__()
		dir(self)
		self.__dict__.update(spec)
		self.filepath = file
		self.command = getattr(self, self.command)

	def close(self):
		pass

	def read(self, params):
		self.print_dd_template(self.filepath, params)

	def write(self, params):
		self.print_dd_template(self.filepath, params)

#args.debugに依存している
#DdがDd(file, spec)で生成されるのだから
#それ以前の部分でdebugの切り替えができるようにするか
#specの後にoptional引数を付けるか
#あと、呼出し側でDd_specへの参照もあるが、これはしょうがないかなあ。
#"Reader"等文字列でspec->initでspec searchするほうが汎用的か？
Dd = Dd_template if args.dd_template else Dd_mmap
if args.dd:
	Dd.DD = args.dd

###############
#ntxbinのハンドリング
# * binfile=header+dataとする
# * 読み書きするdisk側offsetは領域位置を-0x200補正
# * write時
#     何も考えずにそのままフルサイズ書く
# * read時
#     512バイト読んででヘッダチェックしdataサイズを得る
#     appendでデータ部分を書く
#u-bootの扱い
# raw,0x400,u-boot.binとしたほうがいいのかなあ？
# raw skip=0x400,u-boot.binでもいいが…
# さてどうしよう
# いずれにせよ、DdへのI/Fとしてはoptionsになる。
#  Task側でiniの形式→optionsをすべきか？
###############

#やっつけで追加:ntxbin読み込みpatch処理
#dd templateのときはどうしようもないのでどうしよう？
def ntxbin_header_reader(dd, params):
	if params.count:	#カウントがあればそれに従うので補正なし
		return
	#ない場合は読む
	#offset補正はこれ以前に行われているので、
	#既にoffsetがheader位置を意味する
	header = dd.raw_read(params.offset, 512)
	(ntx_magic, ntx_endian, ntx_datasize) = \
		struct.unpack_from("<III", header, 0x1f0)
	if (ntx_magic != 0xffaff5ff or ntx_endian != 0x12345678):
		raise Error("ntx header missing")
	params.count = ntx_datasize + 0x200

#raw形式partをntxbin形式で書くときのhelperがいるが
#想定外なので放置
def ntxbin_header_writer(dd, params):
	pass

#TaskはddへのパラメータobjectとしてのI/Fを持つ
#attrとして規定のfile等を持つ

#specsは共通なのでTaskBuilderで処理してしまうほうがいい
class Task:
	IntegerAttributes = ("bs","offset","count","skip","seek")
	def __init__(self, name, params, specs):
		self.name = name
		self.__dict__.update(specs)
		self.params = params.split(',', self.mode)
		self.options = {}
		#mode > 0の場合は先頭(params[0])が常に"foo=bar zoo"形式options
		if self.mode != 0:
			#"foo" => ("foo",) => tuple len = 1
			for t in (tuple(map(str.strip, x.split('=',1))) for x in self.params[0].split(' ')):
				self.options[t[0]] = t[1] if len(t) == 2 else t[0]
		#optionsで指定した値でattrを上書きする(offset上書き可能)
		#ntxbinのときは、offset上書きしても後で補正される(rawなら無補正)
		self.__dict__.update(self.options)
		#modeによらず末尾(params[-1])が常にfile要素
		self.file = self.params[-1]
		#convert string to integer (hexadecimal supported)
		for k in self.IntegerAttributes:
			if k in self.__dict__:
				self.__dict__[k] = int(self.__dict__[k], 0)
		#diskはmode=0でoffsetなし
		#その他はmode=1でoffsetあり
		if self.mode == 1:
			if "ntxbin" in self.options:
				for key, sign in (('offset', -1), ('count', +1)):
					if getattr(self, key) is not None:
						setattr(self, key, getattr(self, key) + (sign * 0x200))

	#seek等未定義のものはNoneを返す
	def __getattr__(self, name):
		return None

	def __cmp__(self, other):
		return cmp(self.name, other.name)

#TaskBuilderはinitでconcreteなTaskBuilderとして自分自身を構築する
#構築されたconcrete TaskBuilderがcreateによりTask要素を生成する
class TaskBuilder:
	def __init__(self, name, specs):
		self.name = name
		self.specs = dict(specs)

	def create(self, params):
		return Task(self.name, params, self.specs)

	@classmethod
	def Build(cls,raw_tasklist):
		res_dict = dict()
		for ent in raw_tasklist:
			if 'name' in ent:
				key = ent["name"]
				res_dict[key] = TaskBuilder(key, ent)
			elif '$' not in ent:
				print("no 'name' key in dict:")
		return res_dict



#TODO: args.mapfileみたいに引数指定可能とする
#1)オプション指定
#2)カレントの"diskmap.json"
#3)スクリプト本体のパスの"diskmap.json"
#の優先順位で探す

if args.diskmap:
	diskmap_file = args.diskmap
else:
	diskmap_file = "diskmap.json"
	if not os.path.exists(diskmap_file):
		diskmap_file = os.path.normpath(os.path.join(
			os.path.dirname(os.path.abspath(__file__))
			, diskmap_file))

diskmap_taskbuilder = TaskBuilder.Build(load_json(diskmap_file))
#globalなobjectとしてdiskmap_taskbuilderが生成されていないと
#Jobが困る。これはしょうがない
#生成関連をクラス化し、class methodで持つようにすれば
#classの名前空間に押し込めることになるので、少しはマシか？
###############

#jobはdiskmap_taskbuilder,dd_Reader,dd_writeの名前に依存している
#(specで参照する必要がある)

#modeはiniファイルエントリ側のfilename部分のフィールド分割split回数

#<options>,<file>
#数値=binのoffset seek位置
#ntxbin:書くファイルがntxbinである
#とりあえず細かいオプションは放置
#optionsは空白区切りとする(ファイル名に","が使えるように)
#予定
#*任意セクタ書き込み
#  seek,skip,bs,count

#task list(parts)がvalidかどうかはjobでlist全体で判断するしかない
#その一方、diskの有無という判断基準は特定のtaskbuilder固有の事情
#どちらかといえばtasksetという概念で語られるべきもの
#tasksetはjobレベルで単純にlistで実装されている
#builder.create_taskset()みたいにbuilderに移したほうがいいのかも？
#diskを特別扱いし、job.diskにするあたりも含めて再検討

def JobClassInitializer(klass):
	d = {}
	for v in klass.JobSpecs:
		n = v["name"]
		d[n] = v
	klass.JobSpecs = d
	return klass

@JobClassInitializer
class Job:
	#converting from list to dict by class decorator
	#この変換は生成時にlist内包で済む話かもしれない
	JobSpecs = [
		{
			'name': 'inject',
			'taskbuilder': diskmap_taskbuilder,
			'dd_operator_spec': Dd.Writer,
			'ntxbin_helper': None,	#ntxbin_header_writer,
		},
		{
			'name': 'extract',
			'taskbuilder': diskmap_taskbuilder,
			'dd_operator_spec': Dd.Reader,
			'ntxbin_helper': ntxbin_header_reader,
		},
	]

	def __init__(self, specname):
		self.parts = []
		if specname not in self.JobSpecs:
			raise Exception("job spec undefined: "+specname)
		self.__dict__.update(self.JobSpecs[specname])

	def add(self, name, value):
		retval = name in self.taskbuilder
		if retval:
			part = self.taskbuilder[name].create(value)
			if part.name == 'disk':
				self.disk = part
			else:
				self.parts.append(part)
		return retval

	#boolへの型変換っぽい感じでvalidを見られるようにする？
	#__call__()を定義すればinst()でfunction的に呼ばれるようになる
	def is_valid(self):
		return hasattr(self, 'disk')

	#ddへ渡すoption等がちょっと美しくない
	def execute(self):
		print("###### "+self.name+" start ######")
		if self.is_valid():
			with contextlib.closing(Dd(self.disk.file, self.dd_operator_spec)) as dd:
				for part in self.parts:
					if "ntxbin" in part.options and self.ntxbin_helper:
						self.ntxbin_helper(dd, part)
					dd.command(part)
		else:
			print("none of valid part")
		print("###### "+self.name+" done ######")



###############

#generatorにしてもいいのかもしれない
def generate_jobs(config):
	jobs = []
	for sect_name in config.sections():
		try:
			job = Job(sect_name)
		except Exception as e:
			raise Error("unknown section: '{0}'".format(sect_name))

		for (iname,ivalue) in config.items(sect_name):
			if not job.add(iname, ivalue):
				print("unknown task item: '{1}' in section '{0}'".format(sect_name, iname))
		jobs.append(job)
	return jobs


################################
#このあたりはメイン処理

config = configparser.ConfigParser()
config.read(args.config_file, encoding=encoding)

for job in generate_jobs(config):
	job.execute()

###############################
#少し構造が変で
# class内にglobalに参照関係にあるものが存在するので、
# ちょっと困りもの


#ntxbin領域にraw書き込みする時などはこのままでは困るがとりあえず放置


#### kernel等のrawかつサイズ指定しづらいものをどうするか？
#### maxで読んでなんとかするわけだが…
#### u-bootも特にサイズ規定がない
