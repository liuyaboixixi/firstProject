package com.javaproject.demo.controller;

import com.javaproject.demo.dto.CommentDTO;
import com.javaproject.demo.dto.commentCreateDTO;
import com.javaproject.demo.dto.ResultDTO;
import com.javaproject.demo.enums.CommentTypeEnum;
import com.javaproject.demo.exception.CustomizeErrorCode;
import com.javaproject.demo.model.Comment;
import com.javaproject.demo.model.User;
import com.javaproject.demo.service.CommentService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.*;

import javax.servlet.http.HttpServletRequest;
import java.util.List;

@Controller
public class CommentController {
    @Autowired
    private CommentService commentService;

    @ResponseBody
    @RequestMapping(value = "/comment", method = RequestMethod.POST)
    public Object post(@RequestBody commentCreateDTO commentCreateDTO,
                       HttpServletRequest request) {
        User user = (User) request.getSession().getAttribute("user");

        //用户登录判断
        if (user == null) {
            return ResultDTO.errorOf(CustomizeErrorCode.NO_LOGIN);
        }
        //评论不能为空
        if (commentCreateDTO==null||commentCreateDTO.getContent()==null||commentCreateDTO.getContent()==""){
            return ResultDTO.errorOf(CustomizeErrorCode.CONTENT_IS_EMPTY);
        }
        Comment comment = new Comment();
        comment.setParentId(commentCreateDTO.getParentId());
        comment.setContent(commentCreateDTO.getContent());
        comment.setType(commentCreateDTO.getType());
        comment.setGmtModified(System.currentTimeMillis());
        comment.setGmtCreate(System.currentTimeMillis());
        comment.setCommentator(user.getId());
        comment.setLikeCount(0);
        commentService.insert(comment);
        return ResultDTO.okOf();
    }

    //问题回复
    @ResponseBody
    @RequestMapping(value = "/comment/{id}", method = RequestMethod.GET)
    public ResultDTO <List<CommentDTO>> comments(@PathVariable(name = "id")Long id){
        //一级二级回复  使用 listByTargetId
        List<CommentDTO> commentDTOS = commentService.listByTargetId(id, CommentTypeEnum.COMMENT);
        return ResultDTO.okOf(commentDTOS);
    }
}
